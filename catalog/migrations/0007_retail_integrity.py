import copy

from django.db import migrations, models

from catalog.retail_presets import RETAIL_TYPE_PRESETS


def seed_retail_types_and_normalize_stock(apps, schema_editor):
    RetailType = apps.get_model("catalog", "RetailType")
    StockVariant = apps.get_model("catalog", "StockVariant")

    for preset in RETAIL_TYPE_PRESETS:
        values = {
            key: copy.deepcopy(value)
            for key, value in preset.items()
            if key != "key"
        }
        RetailType.objects.update_or_create(key=preset["key"], defaults=values)

    # Preserve every existing reservation before enforcing the invariant.
    # A row with reserved > quantity is normalized by raising its physical
    # quantity, rather than silently discarding customer reservations.
    for variant in StockVariant.objects.filter(reserved__gt=models.F("quantity")):
        variant.quantity = variant.reserved
        variant.save(update_fields=["quantity"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0006_retail_type_model"),
    ]

    operations = [
        migrations.RunPython(
            seed_retail_types_and_normalize_stock,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="stockvariant",
            constraint=models.CheckConstraint(
                condition=models.Q(reserved__lte=models.F("quantity")),
                name="variant_reserved_not_above_quantity",
            ),
        ),
    ]
