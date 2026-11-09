from decimal import Decimal, InvalidOperation

from django.db import migrations


SIZING_SECTIONS = {"men", "women", "kids"}
SIZE_SYSTEM_OPTIONS = [
    {"value": "letters", "label": "Letter sizes (XS, S, M…)"},
    {"value": "numeric", "label": "Numeric sizes (36, 38, 40…)"},
]
LEGACY_SIZE_OPTIONS = {
    "men": ["XS", "S", "M", "L", "XL", "XXL"],
    "women": ["XS", "S", "M", "L", "XL"],
    "kids": ["2Y", "4Y", "6Y", "8Y", "10Y", "12Y", "14Y"],
}


def _is_numeric(value):
    try:
        Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return True


def add_product_sizing(apps, schema_editor):
    RetailType = apps.get_model("catalog", "RetailType")
    CatalogSection = apps.get_model("catalog", "CatalogSection")
    AttributeDef = apps.get_model("catalog", "AttributeDef")
    Product = apps.get_model("catalog", "Product")
    StockVariant = apps.get_model("catalog", "StockVariant")

    clothing_ids = RetailType.objects.filter(key="clothing").values_list("id", flat=True)
    sections = CatalogSection.objects.filter(store__retail_type_id__in=clothing_ids, key__in=SIZING_SECTIONS)
    for section in sections.iterator():
        size_definition = AttributeDef.objects.filter(store_id=section.store_id, section=section, key="size").first()
        if size_definition:
            size_definition.type = "text"
            size_definition.options = []
            size_definition.save(update_fields=["type", "options"])

        AttributeDef.objects.update_or_create(
            store_id=section.store_id,
            section=section,
            key="size_system",
            defaults={
                "label": "Sizing system",
                "type": "select",
                "level": "product",
                "options": SIZE_SYSTEM_OPTIONS,
                "required": True,
                "sort_order": 0,
            },
        )

        for product in Product.objects.filter(section=section).iterator():
            attributes = dict(product.attributes or {})
            if attributes.get("size_system"):
                continue
            sizes = list(
                StockVariant.objects.filter(product=product)
                .exclude(attributes__size__isnull=True)
                .values_list("attributes__size", flat=True)
            )
            attributes["size_system"] = "numeric" if sizes and all(_is_numeric(value) for value in sizes) else "letters"
            product.attributes = attributes
            product.save(update_fields=["attributes"])


def remove_product_sizing(apps, schema_editor):
    RetailType = apps.get_model("catalog", "RetailType")
    CatalogSection = apps.get_model("catalog", "CatalogSection")
    AttributeDef = apps.get_model("catalog", "AttributeDef")
    Product = apps.get_model("catalog", "Product")

    clothing_ids = RetailType.objects.filter(key="clothing").values_list("id", flat=True)
    sections = CatalogSection.objects.filter(store__retail_type_id__in=clothing_ids, key__in=SIZING_SECTIONS)
    for section in sections.iterator():
        AttributeDef.objects.filter(store_id=section.store_id, section=section, key="size_system").delete()
        AttributeDef.objects.filter(store_id=section.store_id, section=section, key="size").update(
            type="select",
            options=[{"value": value, "label": value} for value in LEGACY_SIZE_OPTIONS[section.key]],
        )
        for product in Product.objects.filter(section=section).iterator():
            attributes = dict(product.attributes or {})
            if "size_system" in attributes:
                attributes.pop("size_system")
                product.attributes = attributes
                product.save(update_fields=["attributes"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0008_product_is_featured_product_merchandising_order"),
        ("store", "0008_retail_type_model"),
    ]

    operations = [migrations.RunPython(add_product_sizing, remove_product_sizing)]
