from django.db import migrations


STORE_LIMIT_KEY = "stores.max_count"


def _store_limit_for_plan(plan):
    return 1 if plan.slug == "free" or plan.price == 0 else 3


def seed_store_count_rule(apps, schema_editor):
    EntitlementDefinition = apps.get_model("subscriptions", "EntitlementDefinition")
    PlanEntitlement = apps.get_model("subscriptions", "PlanEntitlement")
    PlanRevision = apps.get_model("subscriptions", "PlanRevision")
    SubscriptionPayment = apps.get_model("subscriptions", "SubscriptionPayment")
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")

    store_limit, _ = EntitlementDefinition.objects.update_or_create(
        key=STORE_LIMIT_KEY,
        defaults={
            "name": "Stores included",
            "description": "The number of online stores a merchant can own at the same time.",
            "value_type": "integer",
            "scope": "account",
            "unit": "stores",
            "allowed_values": [],
            "default_value": 1,
            "is_active": True,
            "sort_order": 5,
        },
    )

    definitions = list(EntitlementDefinition.objects.filter(is_active=True))
    for plan in SubscriptionPlan.objects.all().iterator():
        store_limit_value = _store_limit_for_plan(plan)
        PlanEntitlement.objects.update_or_create(
            plan=plan,
            definition=store_limit,
            defaults={"value": store_limit_value},
        )

        plan.revision += 1
        plan.save(update_fields=["revision"])
        configured = {
            item.definition_id: item.value
            for item in PlanEntitlement.objects.filter(plan=plan)
        }
        entitlements = {
            definition.key: configured.get(definition.pk, definition.default_value)
            for definition in definitions
        }
        snapshot = {
            "id": plan.pk,
            "revision": plan.revision,
            "name": plan.name,
            "slug": plan.slug,
            "description": plan.description,
            "price": str(plan.price),
            "currency": plan.currency,
            "duration_days": plan.duration_days,
            "entitlement_scope": plan.entitlement_scope,
            "features": list(plan.features or []),
            "entitlements": entitlements,
            "is_active": plan.is_active,
            "is_featured": plan.is_featured,
            "sort_order": plan.sort_order,
        }
        PlanRevision.objects.update_or_create(
            plan=plan,
            revision=plan.revision,
            defaults={"snapshot": snapshot},
        )

    for model in (SubscriptionPayment, UserSubscription):
        for record in model.objects.select_related("plan").all().iterator():
            snapshot = dict(record.entitlements_snapshot or {})
            snapshot[STORE_LIMIT_KEY] = _store_limit_for_plan(record.plan) if record.plan else 1
            record.entitlements_snapshot = snapshot
            record.save(update_fields=["entitlements_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0003_subscriptionpayment_entitlements_snapshot_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_store_count_rule, migrations.RunPython.noop),
    ]
