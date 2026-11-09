from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .entitlements import active_subscription_period_q
from .models import SubscriptionLifecycleNotice, UserSubscription
from .services import refresh_expired_subscription


def _event_for_remaining(remaining):
    if remaining <= timedelta(0):
        return SubscriptionLifecycleNotice.Event.EXPIRED
    if remaining <= timedelta(days=3):
        return SubscriptionLifecycleNotice.Event.THREE_DAYS
    if remaining <= timedelta(days=7):
        return SubscriptionLifecycleNotice.Event.SEVEN_DAYS
    return None


def _scope_has_renewal(subscription, now):
    queryset = UserSubscription.objects.filter(
        user_id=subscription.user_id,
        entitlement_scope=subscription.entitlement_scope,
    ).exclude(pk=subscription.pk).filter(active_subscription_period_q(now))
    if subscription.selected_store_id is None:
        queryset = queryset.filter(selected_store__isnull=True)
    else:
        queryset = queryset.filter(selected_store_id=subscription.selected_store_id)
    return queryset.exists()


def dispatch_subscription_lifecycle_notices(now=None):
    """Send each 7-day, 3-day, and expiry notice at most once per period."""
    now = now or timezone.now()
    sent = 0
    candidates = UserSubscription.objects.select_related("user", "plan", "user__merchant_profile").filter(
        status__in=[UserSubscription.Status.ACTIVE, UserSubscription.Status.EXPIRED],
        current_period_end__isnull=False,
        current_period_end__lte=now + timedelta(days=7),
    )
    for candidate in candidates.iterator():
        event = _event_for_remaining(candidate.current_period_end - now)
        if event is None:
            continue
        if event == SubscriptionLifecycleNotice.Event.EXPIRED:
            if _scope_has_renewal(candidate, now):
                continue
            if candidate.status == UserSubscription.Status.ACTIVE:
                candidate = refresh_expired_subscription(candidate)
        elif candidate.status != UserSubscription.Status.ACTIVE:
            continue
        try:
            with transaction.atomic():
                notice = SubscriptionLifecycleNotice.objects.create(
                    subscription=candidate, period_end=candidate.current_period_end, event=event,
                )
        except IntegrityError:
            continue
        from notifications.platform_email import send_subscription_lifecycle_notice
        if not send_subscription_lifecycle_notice(candidate, event):
            notice.delete()
            continue
        sent += 1
    return {"sent": sent}
