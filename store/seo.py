"""Authoritative, request-independent public SEO policy for merchant stores."""

from dataclasses import dataclass

from django.conf import settings

from storefront_factory.models import StorefrontRequest

from .models import StorefrontDomain


@dataclass(frozen=True)
class StorefrontSeoPolicy:
    canonical_origin: str
    indexable: bool
    reason: str


def official_storefront_origin(store) -> str:
    """Return the platform-owned origin without consulting request metadata."""
    base = settings.STOREFRONT_BASE_DOMAIN.strip().lower().strip(".")
    return f"https://{store.slug}.{base}"


def _healthy_primary_custom_domain(store):
    return store.storefront_domains.filter(
        kind=StorefrontDomain.Kind.CUSTOM,
        is_primary=True,
        status=StorefrontDomain.Status.ACTIVE,
        provider_verified=True,
        certificate_status=StorefrontDomain.CertificateStatus.VALID,
        health_verified_at__isnull=False,
    ).only("hostname").first()


def canonical_storefront_origin(store) -> str:
    """Resolve exactly one tenant-owned SEO origin.

    Host, Origin, Referer, forwarded headers, deployment URLs and preview URLs
    are deliberately not accepted as inputs.
    """
    custom = _healthy_primary_custom_domain(store)
    if custom:
        return f"https://{custom.hostname.encode('idna').decode('ascii').lower().rstrip('.')}"
    return official_storefront_origin(store)


def _latest_live_publication(store):
    return store.storefront_requests.filter(
        pipeline_state=StorefrontRequest.PIPELINE_LIVE,
        status=StorefrontRequest.STATUS_CREATED,
        deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
    ).order_by("-completed_at", "-created_at", "-pk").first()


def _canonical_domain_ready(store, origin: str) -> bool:
    hostname = origin.removeprefix("https://")
    return store.storefront_domains.filter(
        hostname=hostname,
        is_primary=True,
        status=StorefrontDomain.Status.ACTIVE,
        provider_verified=True,
        certificate_status=StorefrontDomain.CertificateStatus.VALID,
        health_verified_at__isnull=False,
    ).exists()


def storefront_seo_policy(store, config=None) -> StorefrontSeoPolicy:
    config = config if config is not None else store.config
    origin = canonical_storefront_origin(store)
    checks = (
        (settings.STOREFRONT_INDEXING_ENABLED and not settings.DEBUG, "environment_disabled"),
        (bool(config.allow_search_indexing), "merchant_opt_out"),
        (not store.is_suspended, "store_suspended"),
        (not hasattr(store, "theme_demo_profile"), "preview_store"),
        (_latest_live_publication(store) is not None, "store_unpublished"),
        (_canonical_domain_ready(store, origin), "domain_not_ready"),
        (store.members.filter(role="owner", user__is_active=True).exists(), "owner_inactive"),
    )
    for allowed, reason in checks:
        if not allowed:
            return StorefrontSeoPolicy(origin, False, reason)
    return StorefrontSeoPolicy(origin, True, "indexable")
