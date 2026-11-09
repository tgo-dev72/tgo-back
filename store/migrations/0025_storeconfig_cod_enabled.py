from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("store", "0024_storefront_domains")]

    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="cod_enabled",
            field=models.BooleanField(default=True, help_text="Allow customers to place cash-on-delivery orders."),
        ),
    ]
