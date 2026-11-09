from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import APIException

from store.models import Store, StoreMember

from .entitlements import active_subscription_period_q
from .models import SubscriptionPlan, UserSubscription


class SubscriptionPaymentRequired(APIException):
    status_code = 402
    default_code = "subscription_required"
    default_detail = "Choose a pack to continue using TijaraGO."

    def __init__(self):
        super().__init__({
            "code": self.default_code,
            "detail": self.default_detail,
            "billing_path": "/subscription",
        })


def user_has_active_subscription(user, *, now=None):
    if not user or not user.is_authenticated:
        return False
    return UserSubscription.objects.filter(
        user=user,
    ).filter(active_subscription_period_q(now or timezone.now())).exists()


def store_has_active_subscription(store, *, now=None):
    owner = StoreMember.objects.select_related("user").filter(
        store=store,
        role=StoreMember.Roles.OWNER,
    ).first()
    if owner is None:
        # Orphaned fixtures and partially-created development stores have no
        # commercial account to charge. SEO already treats them as unhealthy.
        return True
    if not UserSubscription.objects.filter(user=owner.user).exists():
        from .services import ensure_free_subscription
        ensure_free_subscription(owner.user)
    return UserSubscription.objects.filter(user=owner.user).filter(
        active_subscription_period_q(now or timezone.now()),
    ).filter(
        Q(entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES)
        | Q(entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE, selected_store=store)
    ).exists()


def _owner_has_access(owner):
    if not UserSubscription.objects.filter(user=owner).exists():
        from .services import ensure_free_subscription
        ensure_free_subscription(owner)
    return user_has_active_subscription(owner)


def merchant_request_has_active_subscription(user, request):
    django_request = getattr(request, "_request", request)
    match = getattr(django_request, "resolver_match", None)
    store_id = match.kwargs.get("store_id") if match else None
    if store_id is not None:
        store = Store.objects.filter(pk=store_id, members__user=user).first()
        if store is None:
            # Let the view's normal membership/404 policy handle unauthorized
            # store IDs without leaking subscription state first.
            return True
        owner = StoreMember.objects.select_related("user").filter(
            store=store, role=StoreMember.Roles.OWNER,
        ).first()
        return True if owner is None else _owner_has_access(owner.user)

    owned = StoreMember.objects.select_related("user").filter(
        user=user, role=StoreMember.Roles.OWNER,
    ).first()
    if owned is not None:
        return _owner_has_access(user)
    if hasattr(user, "merchant_profile"):
        return _owner_has_access(user)

    return any(
        _owner_has_access(member.user)
        for member in StoreMember.objects.select_related("user").filter(
            store__members__user=user, role=StoreMember.Roles.OWNER,
        ).distinct()
    )


def enforce_merchant_subscription(user, request):
    if not merchant_request_has_active_subscription(user, request):
        raise SubscriptionPaymentRequired()
