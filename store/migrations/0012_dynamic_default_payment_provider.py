from django.db import migrations, models

import store.models


class Migration(migrations.Migration):
    dependencies = [("store", "0011_storeconfig_payment_provider")]

    operations = [
        migrations.AlterField(
            model_name="storeconfig",
            name="payment_provider",
            field=models.CharField(
                default=store.models.default_payment_provider,
                help_text="Payment gateway adapter code. Existing attempts retain their original provider.",
                max_length=40,
            ),
        ),
    ]
