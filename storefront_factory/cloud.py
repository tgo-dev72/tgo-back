"""Shared local/hosted worker pipeline: immutable GitHub source → scaffold →
recorded destination → Vercel. New-repo stores carry isolated dependency bundles;
legacy stores retain their original workspace layout and destination.
"""
import base64
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from functools import partial
from urllib.parse import urlparse

import requests
from django.conf import settings

from .scaffold import ScaffoldError, scaffold_storefront
from .domains import uses_vercel_domain
from .bundle import prepare_isolated_bundle, publishable_path
from .repositories import destination, github_token, pin_repository_coordinates, source_repository, validate_coordinates
from .vercel_deploy import (
    VERCEL_API,
    DeployError,
    _attach_domain,
    _create_project,
    _ensure_project_env_vars,
    _ensure_dns_record,
    _get_project,
    _resolve_domain,
    _trigger_deployment,
    _validate_reused_project,
    _verify_provider_domain,
    _vercel_headers,
    _vercel_params,
)

# /tmp is per-instance, so this only helps warm instances; long enough to
# absorb a burst of "create store" calls on the same instance, short enough
# that a stale template isn't served forever after a design-stock update.
SOURCE_CACHE_TTL = 300

_repo_cache = {"path": None, "ts": 0.0, "sha": None, "design": None}

GITHUB_API = "https://api.github.com"
STOREFRONT_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
IMMUTABLE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
SOURCE_REVISION_FILE = ".tijarago-source-revision"


def validate_pipeline_configuration(req=None):
    """Fail before GitHub/provider mutations when delivery cannot succeed."""
    credential_names = ["VERCEL_API_TOKEN"]
    if not uses_vercel_domain(req):
        credential_names.append("CLOUDFLARE_API_TOKEN")
    missing = [
        name
        for name in credential_names
        if not str(getattr(settings, name, "") or "").strip()
    ]
    if missing:
        raise DeployError(f"Storefront delivery is not configured ({', '.join(missing)}).")
    repo = str(getattr(settings, "DESIGN_STOCK_REPO", "") or "").strip()
    if len(repo.split("/")) != 2 or any(not part for part in repo.split("/")):
        raise DeployError("DESIGN_STOCK_REPO must use the owner/repository format.")
    try:
        target = destination(req) if req else (settings.STOREFRONT_DEPLOYMENT_REPO or repo, settings.STOREFRONT_DEPLOYMENT_BRANCH, "apps/check")
        validate_coordinates(*target, req.slug if req else "check")
        if not github_token(source_repository(req)) or not github_token(target[0], write=True):
            raise ValueError("GitHub source/deployment credentials are not configured.")
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    api_url = str(getattr(settings, "STOREFRONT_API_URL", "") or "").strip()
    parsed = urlparse(api_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise DeployError("STOREFRONT_API_URL must be a public HTTPS URL.")
    if not uses_vercel_domain(req) and not str(getattr(settings, "STOREFRONT_BASE_DOMAIN", "") or "").strip():
        raise DeployError("STOREFRONT_BASE_DOMAIN is not configured.")
    release_revision = str(
        getattr(settings, "STOREFRONT_RELEASE_REVISION", "") or ""
    ).strip()
    if release_revision and not IMMUTABLE_REVISION_RE.fullmatch(release_revision):
        raise DeployError("The configured storefront release is not an immutable revision.")
    if target[0] != repo and not (getattr(req, "target_revision", "") or release_revision):
        raise DeployError("A separate deployment repository requires an approved immutable source revision.")


def _github_headers(repository=None, *, write=False):
    try:
        token = github_token(repository or settings.DESIGN_STOCK_REPO, write=write)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


# ── selected design source ──────────────────────────────────────────

def _download_design_stock(log, revision="", design="", *, repository=None, include_dependencies=False) -> Path:
    """Fetch only one design from an immutable design-stock revision.

    Generated storefront apps make the shared repository grow continuously.
    Downloading its entire archive for every creation therefore becomes slower
    over time. The Git database tree/blob API keeps creation cost proportional
    to the selected template instead of the number of existing stores.
    """
    now = time.time()
    repository = repository or settings.DESIGN_STOCK_REPO
    api = partial(_git_api, repository=repository)
    requested_revision = str(
        revision
        or getattr(settings, "STOREFRONT_RELEASE_REVISION", "")
        or ""
    ).strip()
    if requested_revision:
        if not IMMUTABLE_REVISION_RE.fullmatch(requested_revision):
            raise DeployError("The requested storefront release is not an immutable revision.")
        head_sha = requested_revision.lower()
    else:
        head_sha = api("git/refs/heads/main")["object"]["sha"]
    cached_root = Path(_repo_cache["path"]) if _repo_cache["path"] else None
    if (
        cached_root
        and _repo_cache["sha"] == head_sha
        and _repo_cache["design"] == design
        and _repo_cache.get("repository") == repository
        and _repo_cache.get("dependencies") == include_dependencies
        and now - _repo_cache["ts"] < SOURCE_CACHE_TTL
        and (cached_root / "designs" / design).is_dir()
    ):
        return cached_root

    if not STOREFRONT_SLUG_RE.fullmatch(str(design or "")):
        raise ScaffoldError("The selected storefront design is invalid.")
    repo_root = Path(tempfile.mkdtemp(prefix=f"design-source-{head_sha[:8]}-{design}-"))
    prefix = f"designs/{design}/"
    log(f"Loading the selected storefront design ({head_sha[:7]})…")
    try:
        commit = api(f"git/commits/{head_sha}")
        tree_sha = commit.get("tree", {}).get("sha")
        if not tree_sha:
            raise ScaffoldError("The storefront release tree could not be resolved.")
        tree = api(f"git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise ScaffoldError("The storefront release index is incomplete. Please retry.")
        design_entries = [
            entry for entry in tree.get("tree", [])
            if entry.get("type") == "blob"
            and str(entry.get("path", "")).startswith(prefix)
            and publishable_path(str(entry.get("path", "")))
        ]
        if not design_entries:
            raise ScaffoldError(
                f"Design '{design}' is unavailable in storefront release {head_sha[:12]}."
            )
        dependency_entries = [
            entry for entry in tree.get("tree", [])
            if entry.get("type") == "blob"
            and include_dependencies
            and (
                    str(entry.get("path", "")).startswith("packages/")
                    or entry.get("path") in {"package.json", "package-lock.json", "LICENSE", "LICENSE.md", "THIRD_PARTY_NOTICES.md"}
            )
            and publishable_path(str(entry.get("path", "")))
        ]
        entries = design_entries + dependency_entries

        def fetch_blob(entry):
            path = Path(str(entry["path"]))
            if path.is_absolute() or ".." in path.parts or entry.get("mode") == "120000":
                raise ScaffoldError("Unsafe path in the storefront source tree.")
            blob = api(f"git/blobs/{entry['sha']}")
            if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
                raise ScaffoldError("A storefront design file could not be decoded.")
            return str(entry["path"]), base64.b64decode(blob["content"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            files = list(pool.map(fetch_blob, entries))
        for relative_path, content in files:
            destination = repo_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    except Exception:
        shutil.rmtree(repo_root, ignore_errors=True)
        raise

    (repo_root / SOURCE_REVISION_FILE).write_text(head_sha, encoding="ascii")
    log("Loaded the selected storefront design")
    _repo_cache.update(path=str(repo_root), ts=time.time(), sha=head_sha, design=design,
                       repository=repository, dependencies=include_dependencies)
    return repo_root


# ── GitHub push (git database API, no local git) ─────────────────────

def _git_api(req_path, method="GET", *, repository=None, write=False, **kwargs):
    repository = repository or settings.DESIGN_STOCK_REPO
    # GitHub's repository metadata endpoint rejects a trailing slash. Empty
    # paths are used by the private-repository preflight before any writes.
    url = f"{GITHUB_API}/repos/{repository}"
    if req_path:
        url += f"/{req_path}"
    try:
        resp = requests.request(method, url, headers=_github_headers(repository, write=write), timeout=60, **kwargs)
    except requests.RequestException as exc:
        raise DeployError("GitHub is temporarily unavailable.") from exc
    if resp.status_code >= 400:
        raise DeployError(
            f"GitHub API {method} {req_path} failed ({resp.status_code}): {resp.text[:400]}"
        )
    return resp.json()


def _update_main_ref(commit_sha, *, repository=None, branch="main"):
    try:
        return requests.patch(
            f"{GITHUB_API}/repos/{repository or settings.DESIGN_STOCK_REPO}/git/refs/heads/{branch}",
            headers=_github_headers(repository, write=True), json={"sha": commit_sha, "force": False}, timeout=60,
        )
    except requests.RequestException as exc:
        raise DeployError("GitHub is temporarily unavailable.") from exc


def preflight_destination(repository, branch, *, isolated):
    """Read-only checks before blob uploads, hosting changes or DNS writes."""
    if isolated:
        metadata = _git_api("", repository=repository, write=True)
        if not metadata.get("private"):
            raise DeployError("The separate deployment repository must be private.")
    head = _git_api(f"git/refs/heads/{branch}", repository=repository, write=True)
    if isolated:
        root_tree = _git_api(f"git/trees/{head['object']['sha']}", repository=repository, write=True)
        if root_tree.get("truncated") or any(e.get("path") == "package.json" for e in root_tree.get("tree", [])):
            raise DeployError("The deployment repository must not contain a root npm workspace; each store owns its build root.")


def _push_app_to_github(req, repo_root, log):
    """Commit only this app to its recorded repository/branch, without force."""
    repository, branch, app_path = destination(req)
    try:
        validate_coordinates(repository, branch, app_path, req.slug)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    api = partial(_git_api, repository=repository, write=True)
    app_dir = Path(repo_root) / app_path
    if not app_dir.is_dir():
        raise DeployError(f"apps/{req.slug} is missing after scaffold — nothing to push.")

    files = sorted(p for p in app_dir.rglob("*") if p.is_file() and publishable_path(p.relative_to(app_dir).as_posix()))
    if any(p.is_symlink() for p in app_dir.rglob("*")):
        raise DeployError("Symlinks are not permitted in generated storefronts.")
    if not files:
        raise DeployError(f"No files under apps/{req.slug} — nothing to push.")

    def upload(path: Path):
        rel = path.relative_to(repo_root).as_posix()
        payload = {"content": base64.b64encode(path.read_bytes()).decode("ascii"),
                   "encoding": "base64"}
        for attempt in range(3):
            try:
                sha = api("git/blobs", method="POST", json=payload)["sha"]
                return rel, sha
            except DeployError as exc:
                if attempt == 2:
                    raise DeployError(f"Could not upload {rel}: {exc}") from exc
                # Creating a Git blob is content-addressed and safe to retry.
                # GitHub occasionally returns a transient 400 for otherwise
                # valid concurrent blob requests and explicitly asks clients
                # to resubmit them.
                time.sleep(0.5 * (attempt + 1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        blob_map = dict(pool.map(upload, files))
    log(f"Uploaded {len(blob_map)} blobs")

    # Optimistic retries preserve another storefront commit if two workers
    # publish at the same time. Each attempt rebases this app's replacement
    # tree on the newest main head before atomically advancing the ref.
    prefix = f"apps/{req.slug}/"
    commit_sha = ""
    for attempt in range(3):
        head_sha = api(f"git/refs/heads/{branch}")["object"]["sha"]
        log(f"Current main HEAD {head_sha[:7]}")
        tree = api(f"git/trees/{head_sha}?recursive=1")
        if tree.get("truncated"):
            raise DeployError("GitHub returned an incomplete repository tree; publishing stopped safely.")
        existing = {e["path"]: e for e in tree.get("tree", []) if e.get("type") == "blob"}
        # With base_tree, omitted paths are preserved. Explicit null SHAs are
        # therefore required to remove stale files from an earlier scaffold.
        entries = [
            {"path": p, "mode": e.get("mode", "100644"), "type": "blob", "sha": None}
            for p, e in existing.items()
            if p.startswith(prefix) and p not in blob_map
        ] + [
            {"path": rel, "mode": "100644", "type": "blob", "sha": blob_map[rel]}
            for rel in sorted(blob_map)
        ]
        new_tree = api(
            "git/trees", method="POST", json={"base_tree": tree["sha"], "tree": entries},
        )["sha"]
        commit_sha = api("git/commits", method="POST", json={
            "message": f"Add apps/{req.slug} storefront",
            "tree": new_tree,
            "parents": [head_sha],
        })["sha"]
        update = _update_main_ref(commit_sha, repository=repository, branch=branch)
        if update.status_code < 400:
            break
        if update.status_code not in (409, 422) or attempt == 2:
            raise DeployError(
                f"GitHub main update failed ({update.status_code}): {update.text[:400]}"
            )
        log("Another storefront was published concurrently — rebasing this release.")
    req.github_commit_sha = commit_sha
    req.save(update_fields=["github_commit_sha"])
    log(f"Pushed {app_path} to {repository}:{branch} ({commit_sha[:7]})")
    return branch


def remove_github_storefront_app(slug, *, repository=None, branch="main", app_path=None, expected_tree_sha=None):
    """Remove exactly ``apps/<slug>/`` from the shared repository.

    Missing paths are successful no-ops. Ref conflicts are retried against the
    newest main commit so concurrent storefront publishes are preserved.
    """
    slug = str(slug or "").strip()
    if not STOREFRONT_SLUG_RE.fullmatch(slug):
        raise DeployError("The storefront source path is invalid.")
    prefix = f"apps/{slug}/"
    repository = repository or settings.DESIGN_STOCK_REPO
    try:
        validate_coordinates(repository, branch, app_path or prefix.rstrip("/"), slug)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    api = partial(_git_api, repository=repository, write=True)

    for attempt in range(3):
        head_sha = api(f"git/refs/heads/{branch}")["object"]["sha"]
        tree = api(f"git/trees/{head_sha}?recursive=1")
        if tree.get("truncated"):
            raise DeployError("GitHub returned an incomplete repository tree; source cleanup was stopped safely.")
        if expected_tree_sha is not None:
            actual_tree = next((e.get("sha") for e in tree.get("tree", [])
                                if e.get("type") == "tree" and e.get("path") == prefix.rstrip("/")), None)
            if actual_tree is not None and actual_tree != expected_tree_sha:
                raise DeployError("The retained storefront folder changed; cleanup stopped safely.")
        target_blobs = [
            entry
            for entry in tree.get("tree", [])
            if entry.get("type") == "blob" and str(entry.get("path", "")).startswith(prefix)
        ]
        if not target_blobs:
            return False

        deletion_entries = [
            {
                "path": entry["path"],
                "mode": entry.get("mode", "100644"),
                "type": "blob",
                "sha": None,
            }
            for entry in target_blobs
        ]
        new_tree_sha = api(
            "git/trees",
            method="POST",
            json={"base_tree": tree["sha"], "tree": deletion_entries},
        )["sha"]
        commit_sha = api(
            "git/commits",
            method="POST",
            json={
                "message": f"Remove apps/{slug} storefront",
                "tree": new_tree_sha,
                "parents": [head_sha],
            },
        )["sha"]
        update = _update_main_ref(commit_sha, repository=repository, branch=branch)
        if update.status_code < 400:
            return True
        if update.status_code not in (409, 422) or attempt == 2:
            raise DeployError(
                f"GitHub main update failed ({update.status_code}): {update.text[:400]}"
            )

    raise DeployError("GitHub source cleanup could not be completed.")


# ── Vercel project/env/domain + deploy (cloud flavour) ───────────────

def _set_env_vars(req, project_id, log):
    """Build-time VITE_* vars for the storefront's own Vercel project. Derived
    from the request directly — the scaffolded .env lives in /tmp and is not
    guaranteed to survive into a later invocation."""
    values = {
        "VITE_API_URL": getattr(settings, "STOREFRONT_API_URL", ""),
        "VITE_STORE_SLUG": req.store.slug if req.store_id else req.slug,
    }
    if req.domain and "CHANGE-ME" not in req.domain:
        values["VITE_ACTUAL_ORIGIN"] = req.domain

    _ensure_project_env_vars(project_id, values, log)


def deploy_storefront_cloud(req, log, on_state=None) -> str:
    """Create/link the Vercel project, DNS, domain, env vars and trigger a
    production deployment — assumes apps/<slug> was already pushed to main."""
    if not settings.VERCEL_API_TOKEN:
        raise DeployError("VERCEL_API_TOKEN is not set.")

    branch = destination(req)[1]
    project = _get_project(req.slug)
    if project:
        _validate_reused_project(req, project)
        log(f"Vercel project '{req.slug}' already exists — reusing it.")
    else:
        project = _create_project(req, log)

    req.vercel_project_id = project["id"]
    req.save(update_fields=["vercel_project_id"])
    if on_state:
        on_state(req.PIPELINE_PROVISIONING, 55, "deployment.provisioning")

    hostname = _resolve_domain(req, log)
    req.save(update_fields=["domain"])
    if uses_vercel_domain(req):
        _verify_provider_domain(hostname, project["id"], log)
    else:
        req.cloudflare_dns_record_id = _ensure_dns_record(hostname, log) or ""
        req.save(update_fields=["cloudflare_dns_record_id"])
        _attach_domain(hostname, project["id"], log)
    _set_env_vars(req, project["id"], log)
    if on_state:
        on_state(req.PIPELINE_CONFIGURING_DOMAIN, 70, "deployment.configuring_domain")

    def checkpoint_deployment(deployment_id, deployment_url):
        req.vercel_deployment_id = deployment_id
        req.deployed_url = (
            req.domain
            if req.domain and "CHANGE-ME" not in req.domain
            else deployment_url
        )
        req.save(update_fields=["vercel_deployment_id", "deployed_url"])

    deployment = _trigger_deployment(
        req,
        project,
        branch,
        log,
        on_created=checkpoint_deployment,
    )
    req.vercel_deployment_id = deployment.deployment_id
    req._vercel_ready_state = deployment.ready_state
    # The deployment's own .vercel.app URL sits behind Vercel's SSO and is not
    # user-facing. The custom domain is live immediately (wildcard CNAME +
    # attached/verified domain), so surface that instead.
    req.deployed_url = req.domain if req.domain and "CHANGE-ME" not in req.domain else deployment.url
    req.save(update_fields=["vercel_deployment_id", "deployed_url"])
    if on_state:
        on_state(req.PIPELINE_BUILDING, 82, "deployment.building")
    log(f"Deployment URL: {deployment.url}")
    log(f"Public URL: {req.deployed_url}")


# ── orchestrator ─────────────────────────────────────────────────────

def run_pipeline(req, on_log=None, on_state=None) -> str:
    """Full in-cloud scaffold + push + deploy. Returns the deploy log string.
    Raises ScaffoldError/DeployError on failure (the caller persists status).
    If on_log is provided, it is called with the accumulated log text after
    each step so the frontend can show real-time progress."""
    try:
        pin_repository_coordinates(req)
    except ValueError as exc:
        raise DeployError(str(exc)) from exc
    validate_pipeline_configuration(req)
    repository, branch, _ = destination(req)
    # An initialized target branch is required. Detect configuration mistakes
    # before uploading any blobs or mutating hosting/DNS.
    preflight_destination(repository, branch, isolated=req.deployment_layout == "isolated")
    project = _get_project(req.slug)
    if project:
        _validate_reused_project(req, project)
    log_lines: list[str] = []

    def log(msg: str):
        log_lines.append(str(msg))
        if on_log:
            on_log("\n".join(log_lines))

    cached_root = _download_design_stock(log, revision=req.target_revision, design=req.design,
        repository=source_repository(req), include_dependencies=req.deployment_layout == "isolated")
    # Cached source is immutable. Every worker gets its own scaffold directory.
    with tempfile.TemporaryDirectory(prefix="storefront-build-") as build_dir:
        repo_root = Path(build_dir) / "source"
        shutil.copytree(cached_root, repo_root)
        _scaffold_and_publish(req, repo_root, log, on_state)
    return "\n".join(log_lines)


def _scaffold_and_publish(req, repo_root, log, on_state):
    revision_file = repo_root / SOURCE_REVISION_FILE
    if revision_file.is_file():
        source_revision = revision_file.read_text(encoding="ascii").strip().lower()
        if not IMMUTABLE_REVISION_RE.fullmatch(source_revision):
            raise DeployError("The downloaded storefront release could not be verified.")
        if req.target_revision and req.target_revision.lower() != source_revision:
            raise DeployError("The downloaded storefront release does not match the requested revision.")
        if req.target_revision != source_revision:
            req.target_revision = source_revision
            req.save(update_fields=["target_revision"])
    target = repo_root / "apps" / req.slug
    if target.exists():  # retry after a partial success — never reuse a stale app
        shutil.rmtree(target, ignore_errors=True)

    scaffold_storefront(req, repo_root=repo_root, skip_subprocess=True)
    log(f"Scaffolded apps/{req.slug}")
    if on_state:
        on_state(req.PIPELINE_PUBLISHING, 35, "deployment.publishing")

    if req.deployment_layout == "isolated":
        with tempfile.TemporaryDirectory(prefix="storefront-bundle-") as bundle_dir:
            prepare_isolated_bundle(req, repo_root, bundle_dir)
            _push_app_to_github(req, Path(bundle_dir), log)
    else:
        _push_app_to_github(req, repo_root, log)
    if on_state:
        on_state(req.PIPELINE_PROVISIONING, 55, "deployment.provisioning")
    deploy_storefront_cloud(req, log, on_state=on_state)
