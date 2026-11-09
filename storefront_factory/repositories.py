"""Persisted delivery coordinates. Settings select defaults, not migrations."""
import re
from django.conf import settings


def validate_coordinates(repository, branch, app_path, slug):
    # Keep paths narrowly scoped: cleanup must never accept an arbitrary tree.
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
        raise ValueError("GitHub repository must use the owner/repository format.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", branch or ""):
        raise ValueError("Deployment branch must contain letters, digits, hyphens or underscores.")
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug or "") or app_path != f"apps/{slug}":
        raise ValueError("The storefront source path is invalid.")


def destination(req):
    """Unpinned objects are legacy for reads/cleanup; only pin() opts in."""
    return (
        getattr(req, "deployment_repository", "") or settings.DESIGN_STOCK_REPO,
        getattr(req, "deployment_branch", "") or "main",
        getattr(req, "deployment_app_path", "") or f"apps/{req.slug}",
    )


def source_repository(req=None):
    return getattr(req, "source_repository", "") or settings.DESIGN_STOCK_REPO


def github_token(repository, *, write=False):
    if write:
        if repository == settings.DESIGN_STOCK_REPO:
            return settings.GITHUB_TOKEN
        if repository == settings.STOREFRONT_DEPLOYMENT_REPO:
            return settings.STOREFRONT_DEPLOYMENT_TOKEN
        raise ValueError("No write credential is configured for the recorded deployment repository.")
    if repository != settings.DESIGN_STOCK_REPO:
        raise ValueError("The recorded source repository is no longer configured.")
    return settings.STOREFRONT_SOURCE_TOKEN or settings.GITHUB_TOKEN


def pin_repository_coordinates(req):
    """Checkpoint before any provider mutation, including failed first attempts."""
    if not getattr(req, "deployment_repository", ""):
        # Also protects requests created before the schema upgrade if encountered
        # by a worker without backfilled coordinates.
        has_remote = bool(req.github_commit_sha or req.vercel_project_id or req.deployed_url)
        target = settings.STOREFRONT_DEPLOYMENT_REPO if not has_remote and settings.STOREFRONT_NEW_REPO_ENABLED else ""
        req.deployment_repository = target or settings.DESIGN_STOCK_REPO
        req.deployment_branch = settings.STOREFRONT_DEPLOYMENT_BRANCH if target else "main"
        req.deployment_app_path = f"apps/{req.slug}"
        req.deployment_layout = "isolated" if target and target != settings.DESIGN_STOCK_REPO else "legacy"
    if not req.source_repository:
        req.source_repository = settings.DESIGN_STOCK_REPO
    validate_coordinates(*destination(req), req.slug)
    req.save(update_fields=[
        "source_repository", "deployment_repository", "deployment_branch",
        "deployment_app_path", "deployment_layout",
    ])
