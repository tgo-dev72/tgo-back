"""Public storefront addresses, including opt-in provider-hosted domains."""
from django.conf import settings


def default_storefront_url(slug):
    base = "vercel.app" if settings.STOREFRONT_USE_VERCEL_DOMAIN else settings.STOREFRONT_BASE_DOMAIN
    return f"https://{slug}.{base}"


def public_storefront_url(store, config=None):
    """Return the one authoritative, verified customer-facing store URL."""
    config = config if config is not None else getattr(store, "config", None)
    if getattr(settings, "STOREFRONT_DEPLOYMENT_MODE", "cloud") == "local":
        local = store.storefront_requests.exclude(deployed_url="").order_by("-created_at").first()
        if local and local.pipeline_state == local.PIPELINE_LIVE:
            return local.deployed_url
    primary = store.storefront_domains.filter(
        is_primary=True,
        status="active",
        provider_verified=True,
        certificate_status="valid",
        health_verified_at__isnull=False,
    ).first()
    if primary:
        return f"https://{primary.hostname}"
    recorded = store.storefront_requests.exclude(deployed_url="").order_by("-created_at").first()
    if recorded and (recorded.deployed_revision or recorded.last_known_good_revision):
        return recorded.deployed_url
    return default_storefront_url(store.slug)


def uses_vercel_domain(req=None):
    # A recorded address survives a change to the default, just like the
    # repository coordinates. Never replace an existing custom domain.
    domain = getattr(req, "domain", "") if req is not None else ""
    if domain and "CHANGE-ME" not in domain:
        return domain.rstrip("/") == f"https://{req.slug}.vercel.app"
    return settings.STOREFRONT_USE_VERCEL_DOMAIN
