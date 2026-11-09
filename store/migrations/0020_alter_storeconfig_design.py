from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0019_storeconfig_appearance"),
    ]

    operations = [
        migrations.AlterField(
            model_name="storeconfig",
            name="design",
            field=models.CharField(
                default="basic",
                help_text="Business-family storefront key from the design-stock manifest.",
                max_length=50,
            ),
        ),
    ]
