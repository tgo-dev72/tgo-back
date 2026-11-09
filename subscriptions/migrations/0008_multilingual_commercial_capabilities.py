from django.db import migrations, models

import subscriptions.models
class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0007_subscriptionpayment_balance_authorized"),
    ]

    operations = [
        migrations.AddField(
            model_name="entitlementdefinition",
            name="translations",
            field=models.JSONField(blank=True, default=subscriptions.models.default_entitlement_translations),
        ),
    ]
