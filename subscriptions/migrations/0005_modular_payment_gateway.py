from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0004_seed_store_count_rule")]

    operations = [
        migrations.RemoveConstraint(
            model_name="subscriptionpayment",
            name="uniq_subscription_provider_transaction",
        ),
        migrations.RenameField(
            model_name="subscriptionpayment",
            old_name="ycp_token",
            new_name="provider_token",
        ),
        migrations.AddField(
            model_name="subscriptionpayment",
            name="provider",
            field=models.CharField(db_index=True, default="youcanpay", max_length=40),
        ),
        migrations.AddField(
            model_name="subscriptionpayment",
            name="checkout_url",
            field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AlterField(
            model_name="subscriptionwebhookevent",
            name="event_id",
            field=models.CharField(max_length=100),
        ),
        migrations.AddField(
            model_name="subscriptionwebhookevent",
            name="provider",
            field=models.CharField(db_index=True, default="youcanpay", max_length=40),
        ),
        migrations.AddConstraint(
            model_name="subscriptionpayment",
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_transaction_id=""),
                fields=("provider", "provider_transaction_id"),
                name="uniq_subscription_provider_transaction",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionwebhookevent",
            constraint=models.UniqueConstraint(
                fields=("provider", "event_id"),
                name="uniq_subscription_provider_event",
            ),
        ),
    ]
