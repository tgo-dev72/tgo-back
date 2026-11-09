from django.db import migrations


def paid_plans_to_usd(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    SubscriptionPlan.objects.filter(
        price__gt=0,
        currency="MAD",
    ).update(currency="USD")


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0012_paypal_order_integrity")]

    operations = [
        migrations.RunPython(paid_plans_to_usd, migrations.RunPython.noop),
    ]
