import copy

from django.db import migrations

from catalog.retail_presets import RETAIL_TYPE_PRESETS


def restore_retail_types(apps, schema_editor):
    RetailType = apps.get_model("catalog", "RetailType")
    for preset in RETAIL_TYPE_PRESETS:
        values = {
            key: copy.deepcopy(value)
            for key, value in preset.items()
            if key != "key"
        }
        RetailType.objects.update_or_create(key=preset["key"], defaults=values)


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0010_harden_product_inventory"),
    ]

    operations = [
        migrations.RunPython(
            restore_retail_types,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
