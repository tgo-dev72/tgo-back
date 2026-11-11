"""Idempotent bootstrap for commercial configuration removed by a DB flush."""

from decimal import Decimal

from django.db import transaction

from .models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan
from .official_plans import OFFICIAL_PLANS, plan_translations
from .policies import BUSINESS_RULES, PREMIUM_DESIGNS_KEY


FREE_PLAN_TRANSLATIONS = {
    "en": {
        "name": "Free",
        "description": "Start selling with the essential tools for one online store.",
    },
    "fr": {
        "name": "Gratuit",
        "description": "Commencez à vendre avec les outils essentiels pour une boutique en ligne.",
    },
    "ar": {
        "name": "مجاني",
        "description": "ابدأ البيع باستخدام الأدوات الأساسية لمتجر إلكتروني واحد.",
    },
}


@transaction.atomic
def restore_commercial_reference_data():
    capabilities_created = 0
    for rule in BUSINESS_RULES.values():
        capability, created = EntitlementDefinition.objects.get_or_create(
            key=rule.key,
            defaults={
                "name": rule.name,
                "description": rule.description,
                "translations": rule.translations or {},
                "value_type": rule.value_type,
                "scope": rule.scope,
                "unit": rule.unit,
                "allowed_values": list(rule.allowed_values),
                "default_value": rule.default_value,
                "is_active": rule.active,
                "sort_order": rule.sort_order,
            },
        )
        capabilities_created += int(created)
        update_fields = []
        # Restore mandatory contracts and missing customer copy, but preserve
        # platform-admin wording changes and configured plan values.
        for field, value in (
            ("value_type", rule.value_type),
            ("scope", rule.scope),
            ("allowed_values", list(rule.allowed_values)),
            ("is_active", rule.active),
        ):
            if getattr(capability, field) != value:
                setattr(capability, field, value)
                update_fields.append(field)
        if not capability.translations:
            capability.translations = rule.translations or {}
            update_fields.append("translations")
        if update_fields:
            capability.full_clean()
            capability.save(update_fields=[*update_fields, "updated_at"])

    free_plan, plan_created = SubscriptionPlan.objects.get_or_create(
        slug="free",
        defaults={
            "name": "Free",
            "description": "Start selling with the essential tools for one online store.",
            "translations": FREE_PLAN_TRANSLATIONS,
            "price": Decimal("0.00"),
            "currency": "MAD",
            "duration_days": 30,
            "entitlement_scope": SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            "features": [],
            "grants_premium_designs": False,
            "is_active": True,
            "is_featured": False,
            "sort_order": 0,
        },
    )
    protected_updates = []
    for field, value in (
        ("price", Decimal("0.00")),
        ("entitlement_scope", SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES),
        ("grants_premium_designs", False),
        ("is_active", True),
    ):
        if getattr(free_plan, field) != value:
            setattr(free_plan, field, value)
            protected_updates.append(field)
    if not free_plan.translations:
        free_plan.translations = FREE_PLAN_TRANSLATIONS
        free_plan.name = FREE_PLAN_TRANSLATIONS["en"]["name"]
        free_plan.description = FREE_PLAN_TRANSLATIONS["en"]["description"]
        protected_updates.extend(["translations", "name", "description"])
    if protected_updates:
        free_plan.save(update_fields=[*protected_updates, "updated_at"])

    entitlement_rows_created = 0
    definitions = list(EntitlementDefinition.objects.filter(is_active=True))
    for plan in SubscriptionPlan.objects.all():
        for definition in definitions:
            _row, created = PlanEntitlement.objects.get_or_create(
                plan=plan,
                definition=definition,
                defaults={"value": definition.default_value},
            )
            entitlement_rows_created += int(created)
    # The protected compatibility cache follows the governed entitlement.
    PlanEntitlement.objects.filter(
        plan=free_plan,
        definition__key=PREMIUM_DESIGNS_KEY,
    ).update(value=False)
    definitions_by_key = {item.key: item for item in definitions}
    previous = None
    for slug, config in OFFICIAL_PLANS.items():
        translations = plan_translations(config)
        plan, _created = SubscriptionPlan.objects.update_or_create(
            slug=slug,
            defaults={
                "name": translations["en"]["name"],
                "description": translations["en"]["description"],
                "translations": translations,
                "price": config["price"],
                "currency": "MAD",
                "duration_days": 30,
                "entitlement_scope": SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
                "is_active": True,
                "is_featured": config["featured"],
                "sort_order": config["sort_order"],
                "upgrade_from": previous,
            },
        )
        for key, value in config["entitlements"].items():
            definition = definitions_by_key[key]
            PlanEntitlement.objects.update_or_create(
                plan=plan, definition=definition, defaults={"value": value},
            )
        previous = plan
    # Historical Premium subscriptions retain their immutable entitlement
    # snapshots; the legacy offer is simply unavailable for new checkout.
    SubscriptionPlan.objects.filter(slug="premium").update(is_active=False, is_featured=False)
    return {
        "capabilities": EntitlementDefinition.objects.count(),
        "capabilities_created": capabilities_created,
        "plans": SubscriptionPlan.objects.count(),
        "free_plan_created": plan_created,
        "entitlements_created": entitlement_rows_created,
    }
