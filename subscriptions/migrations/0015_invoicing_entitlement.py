from django.db import migrations


INVOICING_KEY = "invoicing.issue"


def add_invoicing_entitlement(apps, schema_editor):
    EntitlementDefinition = apps.get_model("subscriptions", "EntitlementDefinition")
    PlanEntitlement = apps.get_model("subscriptions", "PlanEntitlement")
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")

    definition, _ = EntitlementDefinition.objects.get_or_create(
        key=INVOICING_KEY,
        defaults={
            "name": "Invoice issuance",
            "description": "Create, render, and deliver compliant customer invoices.",
            "translations": {
                "en": {"name": "Invoice issuance", "description": "Create, render, and deliver customer invoices."},
                "fr": {"name": "Émission de factures", "description": "Créez, générez et envoyez des factures aux clients."},
                "ar": {"name": "إصدار الفواتير", "description": "إنشاء فواتير العملاء وتجهيزها وإرسالها."},
            },
            "value_type": "boolean",
            "scope": "store",
            "unit": "",
            "allowed_values": [],
            "default_value": False,
            "is_active": True,
            "sort_order": 32,
        },
    )
    for plan_id in SubscriptionPlan.objects.values_list("id", flat=True).iterator():
        PlanEntitlement.objects.get_or_create(
            plan_id=plan_id,
            definition=definition,
            defaults={"value": False},
        )


def remove_invoicing_entitlement(apps, schema_editor):
    apps.get_model("subscriptions", "EntitlementDefinition").objects.filter(
        key=INVOICING_KEY,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0014_subscriptionpayment_sandbox")]

    operations = [
        migrations.RunPython(add_invoicing_entitlement, remove_invoicing_entitlement),
    ]
