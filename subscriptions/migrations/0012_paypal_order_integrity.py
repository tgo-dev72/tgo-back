from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0011_catalog_import_and_plan_upgrade_path")]

    operations = [
        migrations.AlterField(
            model_name="subscriptionpayment",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("captured", "Captured; activation pending"),
                    ("confirmed", "Confirmed"),
                    ("failed", "Failed"),
                    ("duplicate", "Duplicate payment"),
                    ("canceled", "Canceled"),
                ],
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionpayment",
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_token=""),
                fields=("provider", "provider_token"),
                name="uniq_subscription_provider_order",
            ),
        ),
    ]
