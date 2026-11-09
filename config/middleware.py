"""Security middleware extensions that share the platform origin policy."""

from django.conf import settings
from django.middleware.csrf import CsrfViewMiddleware


PUBLIC_STOREFRONT_API_PREFIXES = (
    "/api/storefront/",
    "/api/catalog/sections/",
    "/api/catalog/products/",
    "/api/orders/",
    "/api/analytics/events/",
)

from .origins import custom_origin_matches_request, is_local_development_origin


class PlatformOriginCsrfMixin:
    """Share dynamic development-origin verification with DRF's CSRF check."""

    def _origin_verified(self, request):
        if super()._origin_verified(request):
            return True
        if custom_origin_matches_request(request):
            return True
        return bool(
            settings.DEBUG
            and is_local_development_origin(request.META.get("HTTP_ORIGIN"))
        )


class PlatformCsrfViewMiddleware(PlatformOriginCsrfMixin, CsrfViewMiddleware):
    """Allow changing RFC1918 development hosts without weakening production."""


class StorefrontOriginBindingMiddleware:
    """Reject an active custom origin claiming another store on actual calls."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .origins import active_custom_origin_store_id
        store_id = active_custom_origin_store_id(request.META.get("HTTP_ORIGIN"), request.path)
        if store_id and request.method != "OPTIONS" and not custom_origin_matches_request(request):
            from django.http import JsonResponse
            return JsonResponse({"detail": "Storefront origin does not match this store."}, status=403)
        if request.method != "OPTIONS" and request.path.startswith(PUBLIC_STOREFRONT_API_PREFIXES):
            from accounts.cookies import resolve_tenant
            from django.http import JsonResponse
            from store.models import Store
            from subscriptions.access import store_has_active_subscription

            slug = resolve_tenant(request)
            store = Store.objects.filter(slug=slug).first() if slug else None
            if store is not None and not hasattr(store, "theme_demo_profile") and not store_has_active_subscription(store):
                response = JsonResponse({
                    "code": "storefront_temporarily_unavailable",
                    "detail": "This store is temporarily unavailable.",
                    "storeName": store.name,
                }, status=402)
                response["Cache-Control"] = "no-store"
                response["Vary"] = "X-Store-Slug"
                return response
        return self.get_response(request)
