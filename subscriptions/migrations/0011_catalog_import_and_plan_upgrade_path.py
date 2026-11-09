from django.db import migrations, models
import django.db.models.deletion


NEW_KEY = "catalog.import"
LEGACY_KEY = "catalog.import_export"


def forward(apps, schema_editor):
    EntitlementDefinition = apps.get_model("subscriptions", "EntitlementDefinition")
    PlanEntitlement = apps.get_model("subscriptions", "PlanEntitlement")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")

    legacy = EntitlementDefinition.objects.filter(key=LEGACY_KEY).first()
    catalog_import, _created = EntitlementDefinition.objects.update_or_create(
        key=NEW_KEY,
        defaults={
            "name": "Catalog import",
            "description": "Import product information using structured files.",
            "translations": {
                "en": {"name": "Catalog import", "description": "Import product information using CSV or Excel files."},
                "fr": {"name": "Import du catalogue", "description": "Importez les produits avec des fichiers CSV ou Excel."},
                "ar": {"name": "استيراد الكتالوج", "description": "استيراد بيانات المنتجات باستخدام ملفات CSV أو Excel."},
            },
            "value_type": "boolean",
            "scope": "store",
            "unit": "",
            "allowed_values": [],
            "default_value": False,
            "is_active": True,
            "sort_order": 55,
        },
    )
    # Import was operational for every plan before it had a dedicated gate.
    # Materialize that existing promise as true for every current plan; the
    # definition default remains false for plans created after this transition.
    plan_ids = apps.get_model("subscriptions", "SubscriptionPlan").objects.values_list("id", flat=True)
    for plan_id in plan_ids:
        PlanEntitlement.objects.update_or_create(
            plan_id=plan_id,
            definition=catalog_import,
            defaults={"value": True},
        )

    for subscription in UserSubscription.objects.exclude(entitlements_snapshot={}):
        snapshot = dict(subscription.entitlements_snapshot or {})
        if NEW_KEY in snapshot:
            continue
        snapshot[NEW_KEY] = True
        subscription.entitlements_snapshot = snapshot
        subscription.save(update_fields=["entitlements_snapshot"])
    if legacy:
        legacy.is_active = False
        legacy.save(update_fields=["is_active", "updated_at"])


def backward(apps, schema_editor):
    EntitlementDefinition = apps.get_model("subscriptions", "EntitlementDefinition")
    PlanEntitlement = apps.get_model("subscriptions", "PlanEntitlement")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    for subscription in UserSubscription.objects.exclude(entitlements_snapshot={}):
        snapshot = dict(subscription.entitlements_snapshot or {})
        snapshot.pop(NEW_KEY, None)
        subscription.entitlements_snapshot = snapshot
        subscription.save(update_fields=["entitlements_snapshot"])
    EntitlementDefinition.objects.filter(key=LEGACY_KEY).update(is_active=True)
    PlanEntitlement.objects.filter(definition__key=NEW_KEY).delete()
    EntitlementDefinition.objects.filter(key=NEW_KEY).delete()


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0010_subscriptionplan_translations")]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="upgrade_from",
            field=models.ForeignKey(
                blank=True,
                help_text="Explicit lower plan in the same commercial upgrade path.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="direct_upgrades",
                to="subscriptions.subscriptionplan",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionplan",
            constraint=models.CheckConstraint(
                condition=~models.Q(("pk", models.F("upgrade_from"))),
                name="subscription_plan_not_own_predecessor",
            ),
        ),
        migrations.RunPython(forward, backward),
    ]
