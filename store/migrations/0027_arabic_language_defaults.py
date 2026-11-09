from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("store", "0026_storeconfig_remove_platform_branding")]

    operations = [
        migrations.AlterField(
            model_name="storeconfig",
            name="default_locale",
            field=models.CharField(
                choices=[("ar", "Arabic"), ("fr", "French"), ("en", "English")],
                default="ar",
                help_text="Initial storefront language for customers without a saved preference.",
                max_length=2,
            ),
        ),
        migrations.AlterField(
            model_name="clientprofile",
            name="preferred_language",
            field=models.CharField(
                choices=[("en", "English"), ("fr", "French"), ("ar", "Arabic")],
                default="ar",
                max_length=2,
            ),
        ),
    ]
