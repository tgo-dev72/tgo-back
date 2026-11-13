from django.conf import settings
from django.db import transaction
from django.utils.crypto import salted_hmac


def tracking_visitor_hash(store, visitor_id):
    value = str(visitor_id or "").strip()
    if not value:
        return ""
    return salted_hmac(
        f"meta-tracking-consent:{store.pk}", value,
        secret=settings.SECRET_KEY, algorithm="sha256",
    ).hexdigest()


@transaction.atomic
def withdraw_tracking_consent(store, visitor_id):
    """Revoke outstanding order measurement and scrub queued user data."""
    from orders.models import Order

    from .models import MetaEventDelivery

    visitor_hash = tracking_visitor_hash(store, visitor_id)
    orders = list(
        Order.objects.select_for_update().filter(
            store=store,
            marketing_tracking_visitor_hash=visitor_hash,
            marketing_tracking_consent=True,
        )
    )
    if not orders:
        return 0

    order_ids = [order.pk for order in orders]
    Order.objects.filter(pk__in=order_ids).update(
        marketing_tracking_consent=False,
        marketing_tracking_visitor_hash="",
    )
    deliveries = MetaEventDelivery.objects.select_for_update().filter(
        order_id__in=order_ids,
        status__in=[MetaEventDelivery.Status.PENDING, MetaEventDelivery.Status.RETRY],
    )
    for delivery in deliveries:
        payload = dict(delivery.payload or {})
        payload.pop("user_data", None)
        delivery.payload = payload
        delivery.status = MetaEventDelivery.Status.CANCELLED
        delivery.next_attempt_at = None
        delivery.last_error_code = "consent_withdrawn"
        delivery.save(update_fields=["payload", "status", "next_attempt_at", "last_error_code"])
    return len(order_ids)
