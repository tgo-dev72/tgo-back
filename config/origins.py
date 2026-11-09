"""Single source of truth for browser origins and local app navigation."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit, urlunsplit

from django.core.cache import cache


FRONTEND_PORTS = {
    "showroom": 5178,
    "dashboard": 5179,
    "platform_admin": 5180,
}

STOREFRONT_API_PREFIXES = (
    "/api/storefront/", "/api/catalog/", "/api/auth/", "/api/checkout/",
    "/api/orders/", "/api/payments/", "/api/reviews/", "/api/contact/",
    "/api/subscribe/", "/api/unsubscribe/", "/api/analytics/",
    "/accounts/customer-", "/accounts/csrf",
)


def is_storefront_api_path(path):
    return any(str(path or "").startswith(prefix) for prefix in STOREFRONT_API_PREFIXES)


def active_custom_origin_store_id(origin, path=""):
    """Resolve an exact verified HTTPS origin; never authorize a wildcard."""
    normalized = normalize_origin(origin)
    if not normalized or not is_storefront_api_path(path):
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        return None
    hostname = (parsed.hostname or "").encode("idna").decode("ascii").lower()
    key = f"storefront-domain-origin:{hostname}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    from store.models import StorefrontDomain
    store_id = StorefrontDomain.objects.filter(
        hostname=hostname, kind=StorefrontDomain.Kind.CUSTOM,
        status=StorefrontDomain.Status.ACTIVE, provider_verified=True,
        certificate_status=StorefrontDomain.CertificateStatus.VALID,
        health_verified_at__isnull=False,
    ).values_list("store_id", flat=True).first()
    cache.set(key, store_id or 0, 60)
    return store_id


def custom_origin_matches_request(request):
    store_id = active_custom_origin_store_id(
        request.META.get("HTTP_ORIGIN"), request.path,
    )
    if not store_id:
        return False
    if request.method == "OPTIONS":
        return True
    slug = request.META.get("HTTP_X_STORE_SLUG", "").strip().lower()
    if not slug:
        return False
    from store.models import Store
    return Store.objects.filter(pk=store_id, slug=slug).exists()

# Used by django-cors-headers. CSRF uses the same policy through the custom
# middleware below because Django does not support regular expressions in
# CSRF_TRUSTED_ORIGINS.
LOCAL_DEVELOPMENT_ORIGIN_REGEX = (
    r"^https?://(?:localhost|127\.0\.0\.1|\[::1\]|10(?:\.\d{1,3}){3}|"
    r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})"
    r":51(?:7[3-9]|8\d|9\d)$"
)


def normalize_origin(value: object) -> str | None:
    """Return a path-free HTTP origin, rejecting credentials and ambiguity."""
    candidate = str(value or "").strip().rstrip("/")
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default_port = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    netloc = f"{host}:{port}" if port and not default_port else host
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def is_local_development_origin(value: object) -> bool:
    """Accept only loopback/RFC1918 browser origins on known Vite ports."""
    origin = normalize_origin(value)
    if not origin:
        return False
    parsed = urlsplit(origin)
    if parsed.port not in range(5173, 5200):
        return False
    hostname = (parsed.hostname or "").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or (
        address.version == 4
        and (
            address in ip_network("10.0.0.0/8")
            or address in ip_network("172.16.0.0/12")
            or address in ip_network("192.168.0.0/16")
        )
    )


def frontend_url_for_origin(
    request_origin: object,
    *,
    app: str,
    fallback: str,
    allow_local: bool,
    path: str = "",
) -> str:
    """Resolve an app URL without ever trusting an arbitrary redirect host."""
    fallback = fallback.rstrip("/")
    if app not in FRONTEND_PORTS or not allow_local or not is_local_development_origin(request_origin):
        return fallback
    parsed = urlsplit(normalize_origin(request_origin) or "")
    host = f"[{parsed.hostname}]" if parsed.hostname and ":" in parsed.hostname else parsed.hostname
    local_path = path if path.startswith("/") else f"/{path}" if path else ""
    return urlunsplit((parsed.scheme, f"{host}:{FRONTEND_PORTS[app]}", local_path, "", ""))


def is_allowed_return_origin(
    candidate: object,
    *,
    configured_origins: list[str] | tuple[str, ...] | set[str],
    allow_local: bool,
) -> bool:
    origin = normalize_origin(candidate)
    if not origin:
        return False
    configured = {normalize_origin(value) for value in configured_origins}
    return origin in configured or (allow_local and is_local_development_origin(origin))
