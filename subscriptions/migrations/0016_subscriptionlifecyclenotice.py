from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0015_invoicing_entitlement")]

    operations = [
        migrations.CreateModel(
            name="SubscriptionLifecycleNotice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_end", models.DateTimeField()),
                ("event", models.CharField(choices=[("seven_days", "Seven days before expiry"), ("three_days", "Three days before expiry"), ("expired", "Pack expired")], max_length=16)),
                ("sent_at", models.DateTimeField(auto_now_add=True)),
                ("subscription", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lifecycle_notices", to="subscriptions.usersubscription")),
            ],
            options={"ordering": ["-sent_at"]},
        ),
        migrations.AddConstraint(
            model_name="subscriptionlifecyclenotice",
            constraint=models.UniqueConstraint(fields=("subscription", "period_end", "event"), name="uniq_subscription_period_notice"),
        ),
    ]
