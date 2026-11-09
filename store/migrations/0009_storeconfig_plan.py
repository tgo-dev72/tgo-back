from django.db import migrations, models


PREMIUM_DESIGNS = ("atlas", "vertex", "nexus")


def set_existing_stores_to_free(apps, schema_editor):
    StoreConfig = apps.get_model("store", "StoreConfig")
    StoreConfig.objects.all().update(plan="free")
    StoreConfig.objects.filter(design__in=PREMIUM_DESIGNS).update(design="nova")


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0008_retail_type_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="plan",
            field=models.CharField(
                choices=[("free", "Free"), ("premium", "Premium")],
                default="free",
                help_text="Platform subscription tier. Existing and newly created stores start on Free.",
                max_length=20,
            ),
        ),
        migrations.RunPython(set_existing_stores_to_free, migrations.RunPython.noop),
    ]
