"""Checkpointed operator workflow for one legacy → isolated repository move.

Commands never edit merchant business data or DNS. The public deployment stays
live while a preview builds. Recovery restores the saved provider link and
production deployment before restoring delivery coordinates. Until finalize or
cancel, normal deploy/delete entry points are blocked by the durable journal.
"""
import copy
import json
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import cloud
from .bundle import prepare_isolated_bundle
from .models import StorefrontRepositoryMigration as Migration, StorefrontRequest
from .repositories import destination, github_token, source_repository, validate_coordinates
from .scaffold import scaffold_storefront
from .services import storefront_health_is_ready
from .vercel_deploy import (
    DeployError, VERCEL_API, _get_project, _validate_reused_project,
    _vercel_headers, _vercel_params, _vercel_read,
)

COORDINATES = (
    "source_repository", "deployment_repository", "deployment_branch",
    "deployment_app_path", "deployment_layout", "github_commit_sha",
    "vercel_project_id", "vercel_deployment_id", "target_revision",
    "deployed_revision", "last_known_good_revision", "last_known_good_deployment_id",
)
CLOSED = {"finalized", "canceled"}


def _provider(path, method="GET", body=None):
    kwargs = dict(headers=_vercel_headers(), params=_vercel_params())
    try:
        response = (_vercel_read(VERCEL_API + path, **kwargs) if method == "GET" else
                    requests.request(method, VERCEL_API + path, json=body, timeout=45, **kwargs))
    except requests.RequestException as exc:
        raise DeployError("Vercel request did not complete; inspect the checkpoint before retrying.") from exc
    if response.status_code not in (200, 201, 202, 204):
        raise DeployError(f"Vercel migration request failed (HTTP {response.status_code}).")
    return response.json() if response.content else {}


def _tree(repository, branch, app_path):
    head = cloud._git_api(f"git/refs/heads/{branch}", repository=repository, write=True)["object"]["sha"]
    return _tree_at_revision(repository, head, app_path)


def _tree_at_revision(repository, revision, app_path):
    tree = cloud._git_api(f"git/trees/{revision}?recursive=1", repository=repository, write=True)
    if tree.get("truncated"):
        raise DeployError("Repository tree is incomplete; migration stopped.")
    return next((e["sha"] for e in tree["tree"] if e["path"] == app_path and e["type"] == "tree"), None)


def _checkpoint(migration, state=None, **values):
    for key, value in values.items():
        setattr(migration, key, value)
    if state:
        migration.state = state
        migration.journal = [*migration.journal, {"state": state, "at": timezone.now().isoformat()}]
    migration.save()


def _confirmed(req, slug):
    if not slug or slug != req.slug:
        raise DeployError("Pass the exact store slug as confirmation.")


def prepare(request_id, *, confirm_slug, retention_hours=24, approval):
    """Acquire the store guard before read-only preflight; no provider writes."""
    if not approval.strip() or retention_hours < 0:
        raise DeployError("Record an approval/reference and a nonnegative retention period.")
    with transaction.atomic():
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        _confirmed(req, confirm_slug)
        if Migration.objects.filter(request=req).exists():
            raise DeployError("This store already has a migration journal; resume it instead.")
        if req.pipeline_state != req.PIPELINE_LIVE or req.deploy_status != req.DEPLOY_DEPLOYED:
            raise DeployError("Only a live storefront with no deployment in progress can migrate.")
        repo, branch, path = destination(req)
        target = settings.STOREFRONT_DEPLOYMENT_REPO
        target_branch = settings.STOREFRONT_DEPLOYMENT_BRANCH
        validate_coordinates(repo, branch, path, req.slug)
        validate_coordinates(target, target_branch, path, req.slug)
        if repo != settings.DESIGN_STOCK_REPO or req.deployment_layout != "legacy" or target == repo:
            raise DeployError("This operation supports legacy source → separate deployment repository only.")
        if target_branch != "main":
            raise DeployError("This migration version requires destination main; custom production branches need a separate plan.")
        revision = req.deployed_revision
        if not cloud.IMMUTABLE_REVISION_RE.fullmatch(revision or "") or revision != settings.STOREFRONT_RELEASE_REVISION:
            raise DeployError("Update the store to the pinned approved source release before migration.")
        if not req.vercel_project_id or not req.github_commit_sha or not req.deployed_url:
            raise DeployError("The live store must have complete recorded provider coordinates.")
        if not github_token(repo, write=True) or not github_token(target, write=True) or not settings.VERCEL_API_TOKEN:
            raise DeployError("Source/destination write credentials and Vercel access must remain configured.")
        snapshot = {k: getattr(req, k) for k in COORDINATES}
        snapshot.update(deployment_repository=repo, deployment_branch=branch, deployment_app_path=path,
                        source_repository=source_repository(req), domain=req.domain, deployed_url=req.deployed_url,
                        target_revision=revision)
        migration = Migration.objects.create(
            request=req, state="pending_preflight", source=snapshot,
            destination={"deployment_repository": target, "deployment_branch": target_branch,
                         "deployment_app_path": path, "deployment_layout": "isolated",
                         "approval": approval, "retention_hours": retention_hours},
        )
    return migration


@contextmanager
def _action(request_id, slug, action):
    with transaction.atomic():
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        _confirmed(req, slug)
        migration = Migration.objects.select_for_update().get(request=req)
        if migration.action_token:
            raise DeployError("Another migration command owns this journal; do not run concurrent commands.")
        if migration.state in CLOSED:
            raise DeployError("This migration is closed.")
        migration.action_token = uuid.uuid4()
        migration.action_started_at = timezone.now()
        migration.last_error = ""
        migration.save()
    try:
        # Merchant edits to delivery/domain identity must never be silently overwritten.
        if req.domain != migration.source["domain"] or req.deployed_url != migration.source["deployed_url"]:
            raise DeployError("The store domain changed since preparation; manual recovery is required.")
        yield migration, req
    except Exception as exc:
        migration.last_error = f"{action}: {type(exc).__name__}; inspect provider state and resume or restore."
        migration.save(update_fields=["last_error", "updated_at"])
        raise
    finally:
        Migration.objects.filter(pk=migration.pk, action_token=migration.action_token).update(action_token=None)
        migration.action_token = None


def _shadow(req, migration, *, isolated):
    shadow = copy.copy(req)
    for key, value in migration.source.items():
        if key in COORDINATES:
            setattr(shadow, key, value)
    if isolated:
        for key, value in migration.destination.items():
            if key in COORDINATES:
                setattr(shadow, key, value)
    # Reuse scaffolding/category relationships without saving transitional
    # coordinates or generated SHAs onto the live request.
    def save(*args, **kwargs):
        if "github_commit_sha" in kwargs.get("update_fields", []):
            migration.destination = {**migration.destination, "github_commit_sha": shadow.github_commit_sha}
            _checkpoint(migration)
    shadow.save = save
    return shadow


def _project(req, migration):
    project = _get_project(migration.source["vercel_project_id"])
    if not project or project.get("id") != req.vercel_project_id:
        raise DeployError("Recorded Vercel project is missing or changed.")
    return project


def _assert_public(req, revision):
    if not storefront_health_is_ready(req.deployed_url, req.slug, revision):
        raise DeployError("The exact expected revision is not healthy at the unchanged public URL.")


def _preview_ready(migration, req):
    hostname = urlparse(migration.candidate_url).hostname or ""
    if not hostname.endswith(".vercel.app") or not migration.candidate_url.startswith("https://"):
        raise DeployError("The candidate must use its provider-issued HTTPS deployment URL.")
    secret = getattr(settings, "STOREFRONT_MIGRATION_PREVIEW_BYPASS_SECRET", "")
    headers = {"x-vercel-protection-bypass": secret} if secret else None
    return storefront_health_is_ready(migration.candidate_url, req.slug,
                                      migration.source["deployed_revision"], headers=headers)


def _candidate(migration, req):
    data = _provider(f"/v13/deployments/{migration.candidate_deployment_id}")
    meta, git = data.get("meta") or {}, data.get("gitSource") or {}
    owner, repository = migration.destination["deployment_repository"].split("/")
    if (data.get("projectId") != req.vercel_project_id
        or data.get("target") != "production"
        or git.get("sha") != migration.destination["github_commit_sha"]
        or git.get("ref") != migration.destination["deployment_branch"]
        or meta.get("tijaragoMigration") != str(migration.pk)
        or meta.get("githubCommitOrg", "").lower() != owner.lower()
        or meta.get("githubRepo", "").lower() != repository.lower()):
        raise DeployError("Candidate provider identity does not match this migration's project/repository/commit.")
    return data


def _alias_deployment(req):
    host = urlparse(req.deployed_url).hostname
    data = _provider(f"/v4/aliases/{host}")
    if data.get("projectId") != req.vercel_project_id:
        raise DeployError("The public domain is not attached to the recorded project.")
    deployment_id = data.get("deploymentId") or (data.get("deployment") or {}).get("id")
    if not deployment_id:
        raise DeployError("The public domain has no recorded serving deployment.")
    return deployment_id


def _point_public(migration, req, deployment_id):
    current = _alias_deployment(req)
    if current not in {migration.source["live_deployment_id"], migration.candidate_deployment_id}:
        raise DeployError("The public deployment changed outside this migration; inspect it before switching traffic.")
    if current != deployment_id:
        data = _provider(f"/v13/deployments/{deployment_id}")
        if data.get("projectId") != req.vercel_project_id or data.get("target") != "production" or data.get("readyState") != "READY":
            raise DeployError("Only this project's READY production builds can serve the store.")
        # Previously promoted builds require rollback; new staged production
        # builds use promote. Preview promotion would rebuild a different artifact.
        path = (f"/v1/projects/{req.vercel_project_id}/rollback/{deployment_id}"
                if data.get("readySubstate") == "PROMOTED" else
                f"/v10/projects/{req.vercel_project_id}/promote/{deployment_id}")
        _provider(path, "POST")
    if _alias_deployment(req) != deployment_id:
        raise DeployError("The requested deployment has not reached the public alias; retry or restore.")
    # Rollback can pause automatic assignment. Preserve the project's original
    # policy so future normal storefront updates behave as before migration.
    original = migration.source.get("autoAssignCustomDomains", True)
    if _project(req, migration).get("autoAssignCustomDomains", True) != original:
        _provider(f"/v9/projects/{req.vercel_project_id}", "PATCH", {"autoAssignCustomDomains": original})


def _preflight(migration, req):
    legacy = _shadow(req, migration, isolated=False)
    project = _project(req, migration)
    _validate_reused_project(legacy, project)
    if (project.get("ssoProtection") or project.get("passwordProtection")) and not getattr(
        settings, "STOREFRONT_MIGRATION_PREVIEW_BYPASS_SECRET", "",
    ):
        raise DeployError("Configure this project's automation bypass for protected preview verification before migrating.")
    if (project.get("link") or {}).get("productionBranch", "main") != "main":
        raise DeployError("A non-main production branch needs a separate migration plan.")
    _assert_public(req, migration.source["deployed_revision"])
    live_id = _alias_deployment(req)
    live = _provider(f"/v13/deployments/{live_id}")
    if live.get("readyState") != "READY":
        raise DeployError("The serving deployment is not READY.")
    source_tree = _tree(*destination(legacy))
    if not source_tree:
        raise DeployError("The original generated folder is missing.")
    git = live.get("gitSource") or {}
    meta = live.get("meta") or {}
    owner, repository = migration.source["deployment_repository"].split("/")
    if (live.get("projectId") != req.vercel_project_id or live.get("target") != "production"
        or git.get("sha") != migration.source["github_commit_sha"]
        or git.get("ref") != migration.source["deployment_branch"]
        or meta.get("githubCommitOrg", "").lower() != owner.lower()
        or meta.get("githubRepo", "").lower() != repository.lower()
        or _tree_at_revision(migration.source["deployment_repository"], git["sha"],
                             migration.source["deployment_app_path"]) != source_tree):
        raise DeployError("The original folder/repository/commit does not match the serving production deployment.")
    target = migration.destination
    cloud.preflight_destination(target["deployment_repository"], target["deployment_branch"], isolated=True)
    if _tree(target["deployment_repository"], target["deployment_branch"], target["deployment_app_path"]):
        raise DeployError("The destination store folder already exists; refuse to overwrite it.")
    snapshot = {**migration.source, "tree_sha": source_tree,
                "live_deployment_id": live_id, "rootDirectory": project["rootDirectory"],
                "link": {k: project["link"].get(k) for k in ("type", "org", "repo", "repoId", "productionBranch")},
                "cloudflare_dns_record_id": req.cloudflare_dns_record_id,
                "autoAssignCustomDomains": project.get("autoAssignCustomDomains", True)}
    _checkpoint(migration, "prepared", source=snapshot)


def _publish(migration, req):
    shadow = _shadow(req, migration, isolated=True)
    target = migration.destination
    existing = _tree(*destination(shadow))
    # An interrupted Git ref update can be adopted only if this journal owns it.
    if existing:
        marker = cloud._git_api(f"contents/{target['deployment_app_path']}/repository-migration.json?ref={target['deployment_branch']}",
                               repository=target["deployment_repository"], write=True)
        import base64
        owner = json.loads(base64.b64decode(marker["content"]))
        if owner != {"migration_id": migration.pk, "store_slug": req.slug, "source_revision": req.deployed_revision}:
            raise DeployError("Destination folder is not owned by this migration.")
    _checkpoint(migration, "publishing")
    cached = cloud._download_design_stock(lambda _: None, revision=shadow.target_revision,
        design=shadow.design, repository=source_repository(shadow), include_dependencies=True)
    with tempfile.TemporaryDirectory(prefix="repository-migration-") as temp:
        root = Path(temp) / "source"
        shutil.copytree(cached, root)
        revision = (root / cloud.SOURCE_REVISION_FILE).read_text().strip()
        if revision != shadow.target_revision:
            raise DeployError("Downloaded source does not match the recorded release.")
        shutil.rmtree(root / "apps" / req.slug, ignore_errors=True)
        scaffold_storefront(shadow, repo_root=root, skip_subprocess=True)
        output = Path(temp) / "bundle"
        prepare_isolated_bundle(shadow, root, output)
        (output / shadow.deployment_app_path / "repository-migration.json").write_text(json.dumps({
            "migration_id": migration.pk, "store_slug": req.slug, "source_revision": req.deployed_revision,
        }))
        cloud._push_app_to_github(shadow, output, lambda _: None)
    migration.destination = {**migration.destination, "tree_sha": _tree(*destination(shadow))}
    _checkpoint(migration, "published")


def _relink(migration, req, *, isolated):
    shadow = _shadow(req, migration, isolated=isolated)
    project = _project(req, migration)
    link = project.get("link") or {}
    repo = destination(shadow)[0]
    current = f"{link.get('org')}/{link.get('repo')}" if link else ""
    allowed = {migration.source["deployment_repository"], migration.destination["deployment_repository"], ""}
    if current not in allowed or project.get("rootDirectory") != migration.source["rootDirectory"]:
        raise DeployError("The Vercel project was changed outside the migration; refusing to overwrite it.")
    if current != repo:
        if current:
            _provider(f"/v9/projects/{project['id']}/link", "DELETE")
        # Same endpoint/body used by Vercel's official `vercel git connect`.
        _provider(f"/v9/projects/{project['id']}/link", "POST", {"type": "github", "repo": repo})
    project = _project(req, migration)
    _validate_reused_project(shadow, project)
    return project


def build(request_id, *, confirm_slug):
    with _action(request_id, confirm_slug, "build") as (m, req):
        if m.state == "pending_preflight":
            _preflight(m, req)
        if m.state in {"prepared", "publishing"}:
            _publish(m, req)
        if m.state not in {"published", "linking", "creating_candidate", "building", "candidate_ready"}:
            raise DeployError("This migration is not ready to build a candidate.")
        if not m.candidate_deployment_id:
            _checkpoint(m, "linking")
            project = _relink(m, req, isolated=True)
            # Recover a lost POST response by a durable metadata identity. Never
            # automatically submit a second build when the result is uncertain.
            deployments = _provider(f"/v6/deployments?projectId={project['id']}&limit=100").get("deployments", [])
            matches = [d for d in deployments if (d.get("meta") or {}).get("tijaragoMigration") == str(m.pk)
                       and d.get("target") == "production"]
            if len(matches) > 1:
                raise DeployError("Multiple migration candidates found; inspect them before continuing.")
            if matches:
                data = matches[0]
            else:
                # A previous create intent without a discoverable result must
                # be restored, not silently retried with a duplicate build.
                if any(e["state"] == "creating_candidate" for e in m.journal):
                    raise DeployError("Candidate submission outcome is unknown; restore before another attempt.")
                _checkpoint(m, "creating_candidate")
                data = _provider("/v13/deployments", "POST", {
                    "name": req.slug, "project": project["id"],
                    "gitSource": {"type": "github", "repoId": project["link"]["repoId"],
                                  "ref": m.destination["deployment_branch"], "sha": m.destination["github_commit_sha"]},
                    "meta": {"tijaragoMigration": str(m.pk)},
                    # Same per-deployment option as `vercel --prod --skip-domain`.
                    # Test the production artifact without changing public aliases.
                    "target": "production", "autoAssignCustomDomains": False,
                })
            _checkpoint(m, "building", candidate_deployment_id=data.get("id") or data["uid"],
                        candidate_url="https://" + data["url"])
        data = _candidate(m, req)
        if data.get("readyState") in {"ERROR", "CANCELED"}:
            raise DeployError("Migration preview build failed; restore the recorded source link.")
        if data.get("readyState") != "READY":
            return m
        if not _preview_ready(m, req):
            raise DeployError("Preview health is unavailable or wrong; do not promote it.")
        _assert_public(req, m.source["deployed_revision"])
        if _alias_deployment(req) != m.source["live_deployment_id"]:
            raise DeployError("The serving deployment changed while preparing migration.")
        _checkpoint(m, "candidate_ready")
        return m


def _save_coordinates(m, req, *, isolated):
    values = {k: m.source[k] for k in COORDINATES}
    if isolated:
        values.update({k: v for k, v in m.destination.items() if k in COORDINATES})
        values.update(vercel_deployment_id=m.candidate_deployment_id,
                      last_known_good_deployment_id=m.candidate_deployment_id)
    else:
        values.update(vercel_deployment_id=m.source["live_deployment_id"],
                      last_known_good_deployment_id=m.source["live_deployment_id"])
    with transaction.atomic():
        locked = StorefrontRequest.objects.select_for_update().get(pk=req.pk)
        if locked.domain != m.source["domain"] or locked.vercel_project_id != m.source["vercel_project_id"]:
            raise DeployError("Store identity changed; coordinates were not written.")
        StorefrontRequest.objects.filter(pk=req.pk).update(**values)
    req.refresh_from_db()


def promote(request_id, *, confirm_slug, evidence):
    if not evidence.strip():
        raise DeployError("Record the successful preview/browser evidence before promotion.")
    with _action(request_id, confirm_slug, "promote") as (m, req):
        if m.state not in {"candidate_ready", "rolled_back", "promoting", "migrated"} or not m.candidate_deployment_id:
            raise DeployError("Verify a migration candidate before promotion.")
        if _candidate(m, req).get("readyState") != "READY":
            raise DeployError("The recorded migration candidate is not READY.")
        if not _preview_ready(m, req):
            raise DeployError("The recorded candidate is not healthy.")
        _checkpoint(m, "promoting")
        _relink(m, req, isolated=True)
        _point_public(m, req, m.candidate_deployment_id)
        _assert_public(req, m.source["deployed_revision"])
        _save_coordinates(m, req, isolated=True)
        _checkpoint(m, "migrated", retain_until=timezone.now() + timedelta(hours=m.destination["retention_hours"]),
                    destination={**m.destination, "verification_evidence": evidence})
        return m


def restore(request_id, *, confirm_slug):
    with _action(request_id, confirm_slug, "restore") as (m, req):
        if not m.source.get("live_deployment_id"):
            raise DeployError("Preflight never completed; cancel the unmodified preparation instead.")
        _checkpoint(m, "restoring")
        _relink(m, req, isolated=False)
        _point_public(m, req, m.source["live_deployment_id"])
        _assert_public(req, m.source["deployed_revision"])
        _save_coordinates(m, req, isolated=False)
        # Restoring a never-promoted preview repairs the link, but does not
        # demonstrate rollback of a successfully migrated public deployment.
        verified = any(entry.get("state") == "migrated" for entry in m.journal)
        _checkpoint(m, "rolled_back", rollback_verified_at=timezone.now() if verified else None)
        return m


def finalize(request_id, *, confirm_slug, approval):
    if not approval.strip():
        raise DeployError("Record approval to end retention and remove the exact old folder.")
    with _action(request_id, confirm_slug, "finalize") as (m, req):
        if m.state not in {"migrated", "cleaning_source"} or not m.rollback_verified_at:
            raise DeployError("Rehearse restoration and re-promote successfully before cleanup.")
        if not m.retain_until or timezone.now() < m.retain_until:
            raise DeployError("The rollback retention period has not elapsed.")
        _validate_reused_project(req, _project(req, m))
        if _alias_deployment(req) != m.candidate_deployment_id:
            raise DeployError("The migrated candidate is not serving; cleanup is forbidden.")
        _assert_public(req, m.source["deployed_revision"])
        if _tree(*destination(req)) != m.destination["tree_sha"]:
            raise DeployError("Destination bundle changed; cleanup is forbidden.")
        _checkpoint(m, "cleaning_source", destination={**m.destination, "cleanup_approval": approval})
        cloud.remove_github_storefront_app(req.slug, repository=m.source["deployment_repository"],
            branch=m.source["deployment_branch"], app_path=m.source["deployment_app_path"],
            expected_tree_sha=m.source["tree_sha"])
        _checkpoint(m, "finalized")
        return m


def cancel(request_id, *, confirm_slug):
    with _action(request_id, confirm_slug, "cancel") as (m, req):
        if m.state == "pending_preflight":
            _checkpoint(m, "canceled")
            return m
        if m.state != "rolled_back":
            raise DeployError("Restore the old deployment/link before canceling a migration.")
        _validate_reused_project(req, _project(req, m))
        if _alias_deployment(req) != m.source["live_deployment_id"]:
            raise DeployError("Original deployment must be serving before cancellation.")
        _assert_public(req, m.source["deployed_revision"])
        target = m.destination
        if not target.get("tree_sha") and _tree(target["deployment_repository"], target["deployment_branch"], target["deployment_app_path"]):
            raise DeployError("Destination publication is incomplete; recover its recorded tree before cancellation cleanup.")
        if target.get("tree_sha"):
            cloud.remove_github_storefront_app(req.slug, repository=m.destination["deployment_repository"],
                branch=m.destination["deployment_branch"], app_path=m.destination["deployment_app_path"],
                expected_tree_sha=m.destination["tree_sha"])
        _checkpoint(m, "canceled")
        return m
