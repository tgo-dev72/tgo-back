from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("store", "0025_storeconfig_cod_enabled")]
    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="remove_platform_branding",
            field=models.BooleanField(
                default=False,
                help_text="Merchant preference; effective only while the store owner has the branding entitlement.",
            ),
        ),
    ]
