import hashlib
import logging
import time
from contextlib import contextmanager
from datetime import timedelta
import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.utils import timezone

from orders.models import Order

from .credentials import decrypt_access_token
from .models import MetaEventDelivery, MetaIntegration, StorefrontEvent


logger = logging.getLogger("commerce_analytics.meta")
META_EVENT_NAMES = {
    StorefrontEvent.EventType.PAGE_VIEW: "PageView",
    StorefrontEvent.EventType.PRODUCT_VIEW: "ViewContent",
    StorefrontEvent.EventType.ADD_TO_CART: "AddToCart",
    StorefrontEvent.EventType.CHECKOUT_STARTED: "InitiateCheckout",
}


def _sha(value):
    normalized = str(value or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _content_data(event, order=None):
    properties = event.properties if event else {}
    data = {
        "currency": str((order.currency if order else properties.get("currency")) or "MAD").upper(),
        "value": float(order.amount if order else properties.get("cart_value", 0) or 0),
        "content_type": "product",
    }
    if event and event.product_id:
        data["content_ids"] = [event.product.ref]
    if event and properties.get("quantity"):
        data["num_items"] = int(properties["quantity"])
    if order:
        data["content_ids"] = list(order.items.values_list("ref", flat=True))
        data["num_items"] = sum(order.items.values_list("quantity", flat=True))
        data["order_id"] = str(order.order_id)
    return data


def _user_data(order=None, event=None):
    values = {}
    if event and event.visitor_hash:
        values["external_id"] = event.visitor_hash
    if order and order.marketing_tracking_consent:
        values.update({"em": _sha(order.customer_email), "ph": _sha(order.customer_phone)})
    return {key: [value] for key, value in values.items() if value}


def queue_storefront_meta_event(event, request, *, consent):
    if not consent:
        return None
    integration = MetaIntegration.objects.filter(store=event.store, enabled=True).first()
    if not integration or not integration.pixel_id or not integration.encrypted_access_token:
        return None
    order = event.order
    if event.event_type == StorefrontEvent.EventType.ORDER_SUBMITTED:
        # A placed COD order is not collected revenue. Purchase is emitted on delivery.
        event_name = "Purchase" if (
            order and order.payment_mode == Order.PaymentMode.ONLINE
            and order.is_paid == Order.IsPaid.CONFIRMED
            and event.properties.get("payment_mode") == "online_paid"
        ) else "OrderPlaced"
    else:
        event_name = META_EVENT_NAMES.get(event.event_type)
    if not event_name:
        return None
    origin = request.headers.get("Origin", "").rstrip("/")
    path = str(event.properties.get("path", "/"))
    source_url = f"{origin}{path}" if origin.startswith("https://") else ""
    payload = {
        "action_source": "website",
        "custom_data": _content_data(event, order),
        "user_data": _user_data(order, event),
    }
    delivery, created = MetaEventDelivery.objects.get_or_create(
        store=event.store, event_id=str(event.event_id), event_name=event_name,
        defaults={
            "integration": integration, "storefront_event": event, "order": order,
            "event_time": int(time.time()), "event_source_url": source_url, "payload": payload,
        },
    )
    if not created and delivery.status != MetaEventDelivery.Status.SENT and delivery.integration_id != integration.pk:
        delivery.integration = integration
        delivery.save(update_fields=["integration"])
    return delivery


def queue_order_lifecycle_event(order, event_name):
    if not order.marketing_tracking_consent:
        return None
    integration = MetaIntegration.objects.filter(store=order.store, enabled=True).first()
    if not integration or not integration.encrypted_access_token:
        return None
    # The public order UUID is shared with the returning browser for Meta's
    # browser/server Purchase deduplication. Other lifecycle IDs stay internal.
    event_id = str(order.order_id) if event_name == "Purchase" else f"order-{order.order_id}-{event_name.lower()}"
    delivery, created = MetaEventDelivery.objects.get_or_create(
        store=order.store, event_id=event_id, event_name=event_name,
        defaults={
            "integration": integration, "order": order,
            "event_time": int(time.time()),
            "event_source_url": "",
            "payload": {"action_source": "website", "custom_data": _content_data(None, order), "user_data": _user_data(order)},
        },
    )
    if not created and delivery.status != MetaEventDelivery.Status.SENT and delivery.integration_id != integration.pk:
        delivery.integration = integration
        delivery.save(update_fields=["integration"])
    return delivery


def safe_send_meta_delivery(delivery_id):
    try:
        return send_meta_delivery(delivery_id)
    except Exception as exc:  # Configuration/decryption errors must not affect commerce.
        logger.exception("Meta CAPI delivery could not start", extra={"delivery_id": delivery_id, "error_code": exc.__class__.__name__})
        return False


def send_meta_delivery(delivery_id, *, session=requests):
    delivery = MetaEventDelivery.objects.select_related("integration", "store", "order").filter(pk=delivery_id).first()
    if not delivery or delivery.status in [MetaEventDelivery.Status.SENT, MetaEventDelivery.Status.CANCELLED]:
        return False
    if delivery.order_id and not delivery.order.marketing_tracking_consent:
        payload = dict(delivery.payload or {})
        payload.pop("user_data", None)
        delivery.payload = payload
        delivery.status = MetaEventDelivery.Status.CANCELLED
        delivery.next_attempt_at = None
        delivery.last_error_code = "consent_withdrawn"
        delivery.save(update_fields=["payload", "status", "next_attempt_at", "last_error_code"])
        return False
    integration = delivery.integration
    if not integration.enabled:
        return False
    if not settings.META_GRAPH_API_VERSION:
        raise ImproperlyConfigured("META_GRAPH_API_VERSION must be configured.")
    body = {
        "data": [{
            "event_name": delivery.event_name,
            "event_time": delivery.event_time,
            "event_id": delivery.event_id,
            "event_source_url": delivery.event_source_url or None,
            **delivery.payload,
        }],
        "access_token": decrypt_access_token(integration.encrypted_access_token),
    }
    body["data"][0] = {key: value for key, value in body["data"][0].items() if value not in (None, "", {}, [])}
    try:
        response = session.post(
            f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{integration.pixel_id}/events",
            json=body, timeout=settings.META_CAPI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = response.json()
        if int(result.get("events_received", 0)) < 1:
            raise requests.RequestException("events_not_received")
    except (requests.RequestException, ValueError) as exc:
        attempts = delivery.attempts + 1
        terminal = attempts >= settings.META_CAPI_MAX_ATTEMPTS
        code = getattr(getattr(exc, "response", None), "status_code", None)
        delivery.status = MetaEventDelivery.Status.FAILED if terminal else MetaEventDelivery.Status.RETRY
        delivery.attempts = attempts
        delivery.next_attempt_at = None if terminal else timezone.now() + timedelta(minutes=min(60, 2 ** attempts))
        delivery.last_error_code = f"http_{code}" if code else exc.__class__.__name__
        delivery.save(update_fields=["status", "attempts", "next_attempt_at", "last_error_code"])
        logger.warning("Meta CAPI delivery failed", extra={"store_id": delivery.store_id, "event_name": delivery.event_name, "attempt": attempts, "error_code": delivery.last_error_code})
        return False
    delivery.status = MetaEventDelivery.Status.SENT
    delivery.attempts += 1
    delivery.sent_at = timezone.now()
    delivery.next_attempt_at = None
    delivery.last_error_code = ""
    delivery.save(update_fields=["status", "attempts", "sent_at", "next_attempt_at", "last_error_code"])
    return True


@contextmanager
def _meta_retry_lock():
    """Serialize retry batches across Vercel and external schedulers."""
    if connection.vendor != "postgresql":
        yield True
        return
    # Session-level advisory locks cover the complete batch, including the
    # outbound request, and are automatically released if the worker exits.
    lock_key = 836_382_746_127_941_035
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [lock_key])
        acquired = bool(cursor.fetchone()[0])
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_key])


def retry_meta_deliveries(limit=100):
    with _meta_retry_lock() as acquired:
        if not acquired:
            return 0
        due = MetaEventDelivery.objects.filter(
            status__in=[MetaEventDelivery.Status.PENDING, MetaEventDelivery.Status.RETRY],
        ).filter(next_attempt_at__isnull=True) | MetaEventDelivery.objects.filter(
            status=MetaEventDelivery.Status.RETRY, next_attempt_at__lte=timezone.now(),
        )
        ids = list(due.order_by("created_at").values_list("id", flat=True)[:limit])
        return sum(1 for delivery_id in ids if safe_send_meta_delivery(delivery_id))


def send_test_event(integration, test_event_code, *, session=requests):
    if not settings.META_GRAPH_API_VERSION:
        raise ImproperlyConfigured("META_GRAPH_API_VERSION must be configured.")
    body = {
        "data": [{"event_name": "PageView", "event_time": int(time.time()), "event_id": f"test-{integration.store_id}-{int(time.time())}", "action_source": "website"}],
        "test_event_code": test_event_code,
        "access_token": decrypt_access_token(integration.encrypted_access_token),
    }
    response = session.post(f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/{integration.pixel_id}/events", json=body, timeout=settings.META_CAPI_TIMEOUT_SECONDS)
    response.raise_for_status()
    return int(response.json().get("events_received", 0)) > 0
