from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0012_order_tax_settings_snapshot")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="invoice_locale",
            field=models.CharField(
                blank=True,
                choices=[("fr", "French"), ("en", "English"), ("ar", "Arabic")],
                default="",
                help_text="Merchant-selected language frozen before invoice issuance.",
                max_length=2,
            ),
        ),
    ]
