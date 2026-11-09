from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from store.models import StoreMember

from .models import EntitlementDefinition, PlanEntitlement, PlanRevision, SubscriptionPlan, UserSubscription
from .policies import PREMIUM_DESIGNS_KEY


def active_subscription_period_q(now=None):
    """The single definition of an effective subscription period.

    Paid access has an expiration timestamp. The protected Free plan is an
    explicit, non-expiring membership represented by a null period end.
    """
    now = now or timezone.now()
    free_plan_id = SubscriptionPlan.objects.filter(slug="free").values_list("id", flat=True).first()
    period = Q(current_period_end__gt=now)
    if free_plan_id is not None:
        period |= Q(plan_id=free_plan_id, current_period_end__isnull=True)
    return Q(status=UserSubscription.Status.ACTIVE) & period


def normalize_entitlement_value(definition, value):
    definition.validate_value(value)
    if definition.value_type == EntitlementDefinition.ValueType.DECIMAL:
        return str(Decimal(str(value)))
    if definition.value_type in {EntitlementDefinition.ValueType.TEXT, EntitlementDefinition.ValueType.CHOICE}:
        return value.strip()
    return value


def plan_entitlement_snapshot(plan, definitions=None):
    definitions = list(definitions) if definitions is not None else list(EntitlementDefinition.objects.filter(is_active=True))
    prefetched = getattr(plan, "_prefetched_objects_cache", {}).get("configured_entitlements")
    configured_items = prefetched if prefetched is not None else PlanEntitlement.objects.filter(
        plan=plan, definition__is_active=True,
    ).select_related("definition")
    configured = {
        item.definition_id: item.value
        for item in configured_items
        if item.definition.is_active
    }
    snapshot = {}
    for definition in definitions:
        if definition.pk in configured:
            snapshot[definition.key] = configured[definition.pk]
        elif definition.key == PREMIUM_DESIGNS_KEY:
            # Legacy plans created outside the platform configurator still use
            # the old flag until their first entitlement-aware revision.
            snapshot[definition.key] = bool(plan.grants_premium_designs)
        else:
            snapshot[definition.key] = definition.default_value
    snapshot.setdefault(PREMIUM_DESIGNS_KEY, bool(plan.grants_premium_designs))
    return snapshot


def plan_revision_snapshot(plan):
    return {
        "id": plan.pk,
        "revision": plan.revision,
        "name": plan.name,
        "slug": plan.slug,
        "description": plan.description,
        "translations": dict(plan.translations),
        "price": str(plan.price),
        "currency": plan.currency,
        "duration_days": plan.duration_days,
        "entitlement_scope": plan.entitlement_scope,
        "features": list(plan.features),
        "entitlements": plan_entitlement_snapshot(plan),
        "is_active": plan.is_active,
        "is_featured": plan.is_featured,
        "sort_order": plan.sort_order,
    }


def record_plan_revision(plan, actor=None):
    return PlanRevision.objects.create(
        plan=plan,
        revision=plan.revision,
        snapshot=plan_revision_snapshot(plan),
        created_by=actor,
    )


@transaction.atomic
def configure_plan_entitlements(plan, values, actor=None, increment_revision=True):
    definitions = {item.key: item for item in EntitlementDefinition.objects.filter(is_active=True)}
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise ValueError(f"Unknown entitlement keys: {', '.join(unknown)}")

    for key, value in values.items():
        definition = definitions[key]
        normalized = normalize_entitlement_value(definition, value)
        PlanEntitlement.objects.update_or_create(
            plan=plan,
            definition=definition,
            defaults={"value": normalized},
        )

    if PREMIUM_DESIGNS_KEY in values:
        plan.grants_premium_designs = bool(values[PREMIUM_DESIGNS_KEY])

    if increment_revision:
        plan.revision += 1
    plan.updated_by = actor
    plan.save(update_fields=["grants_premium_designs", "revision", "updated_by", "updated_at"])
    record_plan_revision(plan, actor)
    return plan


def subscription_entitlements(subscription):
    if subscription.entitlements_snapshot:
        return dict(subscription.entitlements_snapshot)
    if subscription.plan_id:
        return plan_entitlement_snapshot(subscription.plan)
    return {}


def _covered_subscriptions(user, store=None):
    query = UserSubscription.objects.filter(
        user=user,
    ).filter(active_subscription_period_q()).select_related("plan")
    if store is None:
        return query.filter(entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES)
    return query.filter(
        Q(entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES)
        | Q(entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE, selected_store=store)
    )


def resolve_entitlement(user, key, store=None):
    definition = EntitlementDefinition.objects.filter(key=key, is_active=True).first()
    if definition is None:
        return None
    if definition.scope == EntitlementDefinition.Scope.STORE and store is None:
        return definition.default_value
    coverage_store = store if definition.scope == EntitlementDefinition.Scope.STORE else None
    subscriptions = list(_covered_subscriptions(user, coverage_store))
    values = [
        subscription_entitlements(subscription).get(key, definition.default_value)
        for subscription in subscriptions
    ]
    if not values:
        free_plan = SubscriptionPlan.objects.filter(slug="free", is_active=True).first()
        if free_plan is not None:
            return plan_entitlement_snapshot(free_plan, definitions=[definition]).get(
                key,
                definition.default_value,
            )
        return definition.default_value
    if definition.value_type == EntitlementDefinition.ValueType.BOOLEAN:
        return any(bool(value) for value in values)
    if definition.value_type == EntitlementDefinition.ValueType.INTEGER:
        return max(int(value) for value in values)
    if definition.value_type == EntitlementDefinition.ValueType.DECIMAL:
        return str(max(Decimal(str(value)) for value in values))
    # Text/choice capabilities cannot be merged numerically. A store-specific
    # promise overrides an account-wide promise; ties use the newest period
    # deterministically instead of depending on database row order.
    candidates = list(zip(subscriptions, values))
    if coverage_store is not None:
        store_specific = [
            pair for pair in candidates
            if pair[0].entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE
        ]
        if store_specific:
            candidates = store_specific
    _subscription, value = max(
        candidates,
        key=lambda pair: (pair[0].current_period_end, pair[0].pk),
    )
    return value


def has_entitlement(user, key, store=None):
    return bool(resolve_entitlement(user, key, store))


def entitlement_limit(user, key, store=None):
    value = resolve_entitlement(user, key, store)
    return None if value is None else int(value)


def owner_has_store_entitlement(store, key):
    owner_ids = StoreMember.objects.filter(
        store=store,
        role=StoreMember.Roles.OWNER,
    ).values_list("user_id", flat=True)
    return any(has_entitlement(subscription_user, key, store) for subscription_user in owner_ids)
