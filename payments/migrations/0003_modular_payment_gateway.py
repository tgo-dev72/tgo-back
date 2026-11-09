from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0002_decimal_amount")]

    operations = [
        migrations.RenameField(
            model_name="paymenttransaction",
            old_name="ycp_token",
            new_name="provider_token",
        ),
        migrations.AddField(
            model_name="paymenttransaction",
            name="provider",
            field=models.CharField(db_index=True, default="youcanpay", max_length=40),
        ),
        migrations.AddField(
            model_name="paymenttransaction",
            name="provider_transaction_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddConstraint(
            model_name="paymenttransaction",
            constraint=models.UniqueConstraint(
                condition=~models.Q(provider_transaction_id=""),
                fields=("provider", "provider_transaction_id"),
                name="uniq_storefront_provider_transaction",
            ),
        ),
    ]
