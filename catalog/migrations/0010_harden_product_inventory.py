from django.db import migrations


UNSUPPORTED_BOOK_FORMATS = {"ebook", "audiobook"}


def harden_existing_catalog(apps, schema_editor):
    AttributeDef = apps.get_model("catalog", "AttributeDef")
    Product = apps.get_model("catalog", "Product")
    StockVariant = apps.get_model("catalog", "StockVariant")

    for definition in AttributeDef.objects.filter(section__key="books", key="format"):
        cleaned = []
        for option in definition.options or []:
            value = option.get("value") if isinstance(option, dict) else option
            if str(value).strip().lower() not in UNSUPPORTED_BOOK_FORMATS:
                cleaned.append(option)
        if cleaned != definition.options:
            definition.options = cleaned
            definition.save(update_fields=["options"])

    products_without_variants = Product.objects.filter(variants__isnull=True).iterator()
    for product in products_without_variants:
        requires_configuration = AttributeDef.objects.filter(
            section_id=product.section_id,
            level="variant",
            required=True,
        ).exists()
        if requires_configuration:
            if product.is_active:
                Product.objects.filter(pk=product.pk).update(is_active=False)
            continue
        StockVariant.objects.create(
            store_id=product.store_id,
            product_id=product.pk,
            attributes={},
            quantity=0,
            reserved=0,
            extra={},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0009_clothing_product_sizing"),
    ]

    operations = [
        migrations.RunPython(harden_existing_catalog, migrations.RunPython.noop),
    ]
