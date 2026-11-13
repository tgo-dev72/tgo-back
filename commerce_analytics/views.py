from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.crypto import salted_hmac
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from secrets import compare_digest
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from catalog.models import Product, StockVariant
from orders.models import Order
from access_control.services import permitted_store
from store.context import resolve_store
from store.models import Store

from .credentials import encrypt_access_token
from .meta import queue_storefront_meta_event, retry_meta_deliveries, send_test_event
from .models import MetaIntegration, StorefrontEvent
from .privacy import withdraw_tracking_consent
from .serializers import StorefrontEventSerializer
from .services import (
    ANALYTICS_RANGES,
    build_store_report,
    capture_inventory_snapshot,
    purge_expired_storefront_events,
    store_analytics_level,
)


BOT_USER_AGENT_MARKERS = (
    "bot", "crawler", "spider", "slurp", "bingpreview", "facebookexternalhit",
    "whatsapp", "telegrambot", "discordbot",
)


def is_probable_bot(request):
    user_agent = request.headers.get("User-Agent", "").lower()
    return any(marker in user_agent for marker in BOT_USER_AGENT_MARKERS)


class MerchantAnalyticsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, store_id):
        store = permitted_store(request.user, store_id, "analytics.view")
        level = store_analytics_level(store)
        try:
            days = int(request.query_params.get("range", "30"))
        except (TypeError, ValueError):
            return Response({"range": ["Choose a valid reporting range."]}, status=status.HTTP_400_BAD_REQUEST)
        if days not in ANALYTICS_RANGES[level]:
            return Response(
                {
                    "detail": "This reporting range is not included in the store's current plan.",
                    "available_ranges": list(ANALYTICS_RANGES[level]),
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(build_store_report(store, days, level=level))


class StorefrontEventCreateView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_analytics_event"

    def post(self, request):
        if is_probable_bot(request):
            return Response(
                {"accepted": False, "reason": "automated_traffic"},
                status=status.HTTP_202_ACCEPTED,
            )
        store = resolve_store(request)
        serializer = StorefrontEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        product = None
        if data.get("product_id"):
            product = get_object_or_404(Product, store=store, pk=data["product_id"])
        variant = None
        if data.get("variant_id"):
            variant = get_object_or_404(StockVariant, store=store, pk=data["variant_id"])
            if product is not None and variant.product_id != product.pk:
                return Response(
                    {"variant_id": ["Variant does not belong to the selected product."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        order = None
        if data.get("order_id"):
            order = get_object_or_404(Order, store=store, order_id=data["order_id"])

        visitor_id = data.get("visitor_id", "")
        visitor_hash = (
            salted_hmac(
                "commerce-analytics-visitor", visitor_id, secret=settings.SECRET_KEY, algorithm="sha256",
            ).hexdigest()
            if visitor_id else ""
        )
        session_id = data.get("session_id", "")
        session_hash = (
            salted_hmac(
                "commerce-analytics-session", session_id, secret=settings.SECRET_KEY, algorithm="sha256",
            ).hexdigest()
            if session_id else ""
        )
        event, created = StorefrontEvent.objects.get_or_create(
            store=store,
            event_id=data["event_id"],
            defaults={
                "event_type": data["event_type"],
                "visitor_hash": visitor_hash,
                "session_hash": session_hash,
                "product": product,
                "variant": variant,
                "order": order,
                "properties": data.get("properties", {}),
                "occurred_at": data["occurred_at"],
            },
        )
        if not created and (
            event.event_type != data["event_type"]
            or event.product_id != (product.pk if product else None)
            or event.variant_id != (variant.pk if variant else None)
            or event.order_id != (order.pk if order else None)
        ):
            return Response(
                {"detail": "This event identifier was already used for a different event."},
                status=status.HTTP_409_CONFLICT,
            )
        queue_storefront_meta_event(
            event, request, consent=data.get("marketing_tracking_consent", False),
        )
        return Response(
            {"accepted": True, "deduplicated": not created, "event_id": str(event.event_id)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AnalyticsMaintenanceView(APIView):
    """Repository-scheduled daily maintenance, authenticated by Vercel."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        secret = settings.CRON_SECRET
        supplied = request.headers.get("Authorization", "")
        if not secret:
            return Response(
                {"detail": "Analytics maintenance is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not compare_digest(supplied, f"Bearer {secret}"):
            return Response({"detail": "Unauthorized."}, status=status.HTTP_401_UNAUTHORIZED)

        snapshot_count = sum(
            capture_inventory_snapshot(store)
            for store in Store.objects.all().iterator()
        )
        deleted_events = purge_expired_storefront_events(
            settings.ANALYTICS_EVENT_RETENTION_DAYS,
        )
        meta_deliveries = retry_meta_deliveries()
        return Response({
            "ok": True,
            "inventory_snapshots": snapshot_count,
            "expired_events_deleted": deleted_events,
            "meta_deliveries_sent": meta_deliveries,
        })


def _meta_state(integration):
    configured = bool(integration.pixel_id and integration.encrypted_access_token)
    return {
        "enabled": integration.enabled,
        "pixel_id": integration.pixel_id,
        "token_configured": bool(integration.encrypted_access_token),
        "status": "connected" if integration.enabled and configured else "configured" if configured else "disconnected",
        "last_tested_at": integration.last_tested_at,
        "last_test_succeeded": integration.last_test_succeeded,
        "last_error_code": integration.last_error_code,
    }


class MetaIntegrationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, store_id):
        store = permitted_store(request.user, store_id, "marketing.manage")
        integration, _ = MetaIntegration.objects.get_or_create(store=store)
        return Response(_meta_state(integration))

    def patch(self, request, store_id):
        store = permitted_store(request.user, store_id, "marketing.manage")
        integration, _ = MetaIntegration.objects.get_or_create(store=store)
        pixel_id = str(request.data.get("pixel_id", integration.pixel_id)).strip()
        if pixel_id and (not pixel_id.isdigit() or not 5 <= len(pixel_id) <= 32):
            return Response({"pixel_id": ["Enter a valid numeric Meta Pixel/Dataset ID."]}, status=400)
        token = request.data.get("access_token")
        if token is not None:
            token = str(token).strip()
            if not token:
                return Response({"access_token": ["Use disconnect to remove the credential."]}, status=400)
            try:
                integration.encrypted_access_token = encrypt_access_token(token)
            except ImproperlyConfigured:
                return Response(
                    {"detail": "Meta credential encryption is not configured on the platform."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            integration.token_configured_at = timezone.now()
        enabled = bool(request.data.get("enabled", integration.enabled))
        if enabled and (not pixel_id or not integration.encrypted_access_token):
            return Response({"detail": "A Pixel/Dataset ID and access token are required before enabling Meta tracking."}, status=400)
        integration.pixel_id = pixel_id
        integration.enabled = enabled
        integration.last_error_code = ""
        integration.save()
        return Response(_meta_state(integration))

    def delete(self, request, store_id):
        store = permitted_store(request.user, store_id, "marketing.manage")
        integration, _ = MetaIntegration.objects.get_or_create(store=store)
        integration.enabled = False
        integration.pixel_id = ""
        integration.encrypted_access_token = ""
        integration.token_configured_at = None
        integration.last_test_succeeded = False
        integration.last_error_code = ""
        integration.save()
        return Response(_meta_state(integration))


class MetaIntegrationTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, store_id):
        store = permitted_store(request.user, store_id, "marketing.manage")
        integration = get_object_or_404(MetaIntegration, store=store)
        code = str(request.data.get("test_event_code", "")).strip()
        if not code:
            return Response({"test_event_code": ["Enter the Test Events code from Meta Events Manager."]}, status=400)
        try:
            ok = send_test_event(integration, code)
            error = "" if ok else "events_not_received"
        except Exception as exc:
            ok, error = False, exc.__class__.__name__
        integration.last_tested_at = timezone.now()
        integration.last_test_succeeded = ok
        integration.last_error_code = error
        integration.save(update_fields=["last_tested_at", "last_test_succeeded", "last_error_code", "updated_at"])
        return Response(_meta_state(integration), status=200 if ok else 502)


class MetaDeliveryRetryView(APIView):
    """Frequent CAPI retry worker, isolated from daily analytics maintenance."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        secret = settings.CRON_SECRET
        supplied = request.headers.get("Authorization", "")
        if not secret:
            return Response({"detail": "Meta retry maintenance is not configured."}, status=503)
        if not compare_digest(supplied, f"Bearer {secret}"):
            return Response({"detail": "Unauthorized."}, status=401)
        return Response({"ok": True, "meta_deliveries_sent": retry_meta_deliveries()})


class MetaConsentView(APIView):
    """Record withdrawal without retaining a raw browser identifier."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_analytics_event"

    def post(self, request):
        store = resolve_store(request)
        visitor_id = str(request.data.get("visitor_id", "")).strip()
        granted = request.data.get("granted") is True
        if not visitor_id or len(visitor_id) > 128:
            return Response({"visitor_id": ["A valid consent identifier is required."]}, status=400)
        if granted:
            # Grant is attached only to future checkout submissions; never
            # retroactively opt an existing order into advertising tracking.
            return Response(status=204)
        withdraw_tracking_consent(store, visitor_id)
        return Response(status=204)
