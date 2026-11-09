"""Phase 2 services: run the full scaffold→deploy pipeline with status
bookkeeping, and sync a request's categories from its store's catalog sections.

Shared by the API views (and intentionally thin — the same logic the admin
actions implement, extracted so the API and admin can't drift)."""
from urllib.parse import urlparse

import requests
from django.conf import settings

from storefront_factory.cloud import remove_github_storefront_app, run_pipeline
from storefront_factory.vercel_deploy import (
    DeployError,
    get_deployment_state,
    remove_cloudflare_dns_record,
    remove_vercel_project,
)
from storefront_factory.deployment_state import heartbeat_deployment, record_deployment_state
from storefront_factory.repositories import destination, github_token, validate_coordinates
from storefront_factory.domains import uses_vercel_domain


class InfrastructureCleanupError(RuntimeError):
    """Remote storefront resources could not be fully removed."""


def cleanup_storefront_infrastructure(deployments):
    """Remove platform-managed DNS, hosting, and generated source code.

    The operation is deliberately idempotent: missing records/projects count
    as success, so a partially completed deletion can be retried safely.
    Custom domains are not removed because their DNS is not owned by the
    platform; only ``<slug>.<STOREFRONT_BASE_DOMAIN>`` CNAMEs are in scope.
    """
    from .local_preview import stop_local_preview

    local_deployments = [
        deployment for deployment in deployments
        if urlparse(deployment.deployed_url or "").hostname in {"127.0.0.1", "localhost"}
        and not deployment.vercel_project_id and not deployment.vercel_deployment_id
    ]
    deployments = [deployment for deployment in deployments if deployment not in local_deployments]
    if getattr(settings, "STOREFRONT_DEPLOYMENT_MODE", "cloud") == "local" and any(
        deployment.vercel_project_id or deployment.vercel_deployment_id
        or (deployment.deployed_url and urlparse(deployment.deployed_url).hostname not in {"127.0.0.1", "localhost"})
        for deployment in deployments
    ):
        raise InfrastructureCleanupError("Hosted storefront cleanup is disabled in local mode.")
    for deployment in local_deployments:
        stop_local_preview(deployment.pk)
    remote_statuses = {"deploying", "deployed", "failed"}
    candidates = [
        deployment
        for deployment in deployments
        if deployment.vercel_project_id
        or deployment.deployed_url
        or deployment.deploy_status in remote_statuses
    ]
    if not candidates:
        return {"dns_records": 0, "vercel_projects": 0, "github_apps": 0}

    project_ids = []
    hostnames = []
    github_targets = []
    for deployment in candidates:
        project_id = deployment.vercel_project_id or deployment.slug
        if project_id not in project_ids:
            project_ids.append(project_id)

        expected_hostname = f"{deployment.slug}.{settings.STOREFRONT_BASE_DOMAIN}".lower()
        configured_hostname = urlparse(deployment.domain or "").hostname
        hostname = (configured_hostname or expected_hostname).rstrip(".").lower()
        if not uses_vercel_domain(deployment) and hostname == expected_hostname and hostname not in hostnames:
            hostnames.append(hostname)
        target = (*destination(deployment), deployment.slug)
        if target not in github_targets:
            github_targets.append(target)

    # Validate both credentials before mutating either provider, reducing the
    # chance of a preventable half-cleanup.
    if project_ids and not settings.VERCEL_API_TOKEN:
        raise InfrastructureCleanupError(
            "Vercel cleanup is required, but VERCEL_API_TOKEN is not configured."
        )
    if hostnames and not settings.CLOUDFLARE_API_TOKEN:
        raise InfrastructureCleanupError(
            "Cloudflare cleanup is required, but CLOUDFLARE_API_TOKEN is not configured."
        )
    try:
        for repo, branch, app_path, slug in github_targets:
            validate_coordinates(repo, branch, app_path, slug)
            if not github_token(repo, write=True):
                raise ValueError("GitHub cleanup requires a write credential for the recorded repository.")
    except ValueError as exc:
        raise InfrastructureCleanupError(str(exc)) from exc

    removed_dns = 0
    removed_projects = 0
    removed_github_apps = 0
    try:
        # Stop routing traffic first, then remove hosting, then its generated
        # source. If any provider fails, the database remains intact and the
        # idempotent sequence can be retried safely.
        for hostname in hostnames:
            removed_dns += remove_cloudflare_dns_record(hostname)
        for project_id in project_ids:
            removed_projects += int(remove_vercel_project(project_id))
        for repo, branch, app_path, slug in github_targets:
            removed_github_apps += int(remove_github_storefront_app(
                slug, repository=repo, branch=branch, app_path=app_path,
            ))
    except DeployError as exc:
        raise InfrastructureCleanupError(str(exc)) from exc

    return {
        "dns_records": removed_dns,
        "vercel_projects": removed_projects,
        "github_apps": removed_github_apps,
    }


def run_cloud_pipeline(req, on_state=None) -> str:
    """Scaffold + push + deploy entirely from the serverless runtime — the
    showroom's single 'scaffold' call ends with a live, deployed storefront.
    Persists every status field the way run_scaffold + run_deploy would have,
    back to back. Raises the underlying error on failure — callers mark
    FAILED."""
    req.status = req.STATUS_SCAFFOLDING
    req.deploy_status = req.DEPLOY_DEPLOYING
    req.save(update_fields=["status", "deploy_status"])

    def on_progress(log_text):
        req.log = log_text
        req.deploy_log = log_text
        req.save(update_fields=["log", "deploy_log"])

    log_text = run_pipeline(req, on_log=on_progress, on_state=on_state)
    ready_state = getattr(req, "_vercel_ready_state", "QUEUED")
    # Provider readiness is necessary but not sufficient: the public domain
    # must also pass an HTTPS health check before merchants see "live".
    req.status = req.STATUS_SCAFFOLDING
    req.deploy_status = req.DEPLOY_DEPLOYING
    req.log = log_text
    req.deploy_log = log_text
    req.save(update_fields=[
        "status", "deploy_status", "log", "deploy_log",
        "app_path", "port", "vercel_project_id", "vercel_deployment_id",
        "deployed_url", "domain",
    ])
    if ready_state == "READY" and on_state:
        on_state(req.PIPELINE_VERIFYING, 94, "deployment.verifying")
    return log_text


def storefront_health_is_ready(url, expected_slug=None, expected_revision=None, *, headers=None):
    if not url:
        return False
    health_url = f"{str(url).rstrip('/')}/_tijarago/health.json"
    try:
        response = requests.get(
            health_url,
            timeout=getattr(settings, "STOREFRONT_HEALTHCHECK_TIMEOUT", 15),
            # Never forward a project-scoped preview bypass to a redirect host.
            allow_redirects=not bool(headers),
            **({"headers": headers} if headers else {}),
        )
        if not 200 <= response.status_code < 300:
            return False
        payload = response.json()
    except (requests.RequestException, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and bool(payload.get("store_slug"))
        and (expected_slug is None or payload.get("store_slug") == expected_slug)
        and (
            expected_revision is None
            or payload.get("release_revision") == expected_revision
        )
    )


def refresh_deployment_status(req, *, expected_task_id=None):
    """Refresh a non-terminal deployment while the showroom polls.

    Transient provider errors leave the request as deploying; they must not
    turn a healthy but slow Vercel build into a permanent platform failure.
    """
    if expected_task_id is not None and str(req.task_id or "") != str(expected_task_id):
        return req
    if req.deploy_status != req.DEPLOY_DEPLOYING or not req.vercel_deployment_id:
        return req
    try:
        state = get_deployment_state(req.vercel_deployment_id)
    except DeployError:
        heartbeat_deployment(req.pk, expected_task_id=expected_task_id)
        return req

    if state == "READY":
        if req.pipeline_state != req.PIPELINE_VERIFYING:
            record_deployment_state(
                req.pk,
                req.PIPELINE_VERIFYING,
                message="deployment.verifying",
                expected_task_id=expected_task_id,
            )
        if storefront_health_is_ready(
            req.deployed_url,
            req.slug,
            req.target_revision or None,
        ):
            record_deployment_state(
                req.pk,
                req.PIPELINE_LIVE,
                message="deployment.live",
                expected_task_id=expected_task_id,
            )
        else:
            heartbeat_deployment(req.pk, expected_task_id=expected_task_id)
    elif state in ("ERROR", "CANCELED"):
        message = f"Vercel deployment ended with status {state}."
        req.log = f"{req.log}\n{message}".strip()
        req.deploy_log = f"{req.deploy_log}\n{message}".strip()
        req.save(update_fields=["log", "deploy_log"])
        record_deployment_state(
            req.pk,
            req.PIPELINE_FAILED,
            message="deployment.failed",
            internal_error=message,
            expected_task_id=expected_task_id,
        )
    elif req.pipeline_state != req.PIPELINE_BUILDING:
        record_deployment_state(
            req.pk,
            req.PIPELINE_BUILDING,
            message="deployment.building",
            expected_task_id=expected_task_id,
        )
    else:
        heartbeat_deployment(req.pk, expected_task_id=expected_task_id)
    req.refresh_from_db()
    return req


def sync_categories_from_store(req) -> int:
    """Seed/refresh the request's StorefrontCategory rows from the store's
    catalog sections (the storefront's build-time taxonomy fallback).

    Falls back to three default categories when the store has no catalog
    sections yet, so a brand-new request can always be scaffolded into a
    runnable storefront instead of erroring on an empty taxonomy."""
    if not req.store_id:
        return 0
    from storefront_factory.models import ICON_CHOICES, StorefrontCategory

    valid_icons = {icon for icon, _ in ICON_CHOICES}
    sections = list(req.store.catalog_sections.all())
    if not sections:
        sections = [
            type("_Sec", (), {"key": "men", "label": "Men", "icon": "FaTshirt:fa"}),
            type("_Sec", (), {"key": "women", "label": "Women", "icon": "FaBagShopping:fa6"}),
            type("_Sec", (), {"key": "accessories", "label": "Accessories", "icon": "FaBoxOpen:fa"}),
        ]
    count = 0
    for section in sections:
        icon = section.icon if section.icon in valid_icons else "FaBoxOpen:fa"
        StorefrontCategory.objects.update_or_create(
            storefront=req,
            key=section.key,
            defaults={"label": section.label, "icon": icon},
        )
        count += 1

    # Prune categories that no longer exist in the store's catalog, so a
    # synced taxonomy never drifts from the real sections.
    active_keys = {s.key for s in sections}
    req.categories.exclude(key__in=active_keys).delete()
    return count
