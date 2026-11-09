from itertools import product as option_combinations

from django.db import migrations


PREFIX = "SHOWCASE-LUX-"
COLOR_SWATCHES = {
    "Ivory": "#eee5d5",
    "Black": "#1b1b1b",
    "Indigo": "#263554",
    "Camel": "#b78b5a",
    "Olive": "#555441",
}
PRODUCTS = (
    ("001", "lux-linen-camp-shirt", "Linen Camp Shirt", "Airy linen with a relaxed camp collar, finished for warm days and understated evenings.", "640.00", "790.00", "fashion-linen-shirt", ("S", "M", "L", "XL"), ("Ivory", "Black")),
    ("002", "lux-selvedge-denim-jacket", "Selvedge Denim Jacket", "Structured dark denim with clean copper hardware and a silhouette designed to age beautifully.", "1200.00", None, "denim-jacket", ("M", "L", "XL"), ("Indigo", "Black")),
    ("003", "lux-silk-slip-dress", "Silk Slip Dress", "A fluid satin silhouette with a softly draped neckline and an elegant, effortless fall.", "1450.00", "1690.00", "silk-dress", ("XS", "S", "M", "L"), ("Ivory", "Black")),
    ("004", "lux-sand-trench-coat", "Sand Trench Coat", "A timeless long-line trench in a warm neutral tone, tailored for polished everyday layering.", "1890.00", None, "fashion-trench", ("S", "M", "L"), ("Camel", "Black")),
    ("005", "lux-lightweight-puffer-jacket", "Lightweight Puffer Jacket", "Refined warmth without bulk, with a clean stand collar and tonal hardware.", "980.00", "1150.00", "fashion-puffer", ("S", "M", "L", "XL"), ("Olive", "Black")),
    ("006", "lux-signature-graphic-hoodie", "Signature Graphic Hoodie", "A heavyweight cotton hoodie with a restrained hand-drawn motif and a soft brushed interior.", "520.00", None, "fashion-hoodie", ("S", "M", "L", "XL"), ("Ivory", "Black")),
)


def seed_lux_showcase(apps, schema_editor):
    Store = apps.get_model("store", "Store")
    CatalogSection = apps.get_model("catalog", "CatalogSection")
    Product = apps.get_model("catalog", "Product")
    ProductImage = apps.get_model("catalog", "ProductImage")
    StockVariant = apps.get_model("catalog", "StockVariant")

    store = Store.objects.filter(slug="lux").first()
    if store is None:
        return
    section = CatalogSection.objects.filter(store=store, key="products").first()
    if section is None:
        section = CatalogSection.objects.create(
            store=store,
            key="products",
            label="Products",
            icon="FaBoxOpen:fa",
            sort_order=15,
        )

    seeded = []
    for position, (suffix, slug, name, description, price, compare_at, image_name, sizes, colors) in enumerate(PRODUCTS, start=1):
        ref = f"{PREFIX}{suffix}"
        variant_options = [
            {"key": "size", "label": "Size", "type": "select", "values": list(sizes)},
            {
                "key": "color",
                "label": "Color",
                "type": "color",
                "values": [
                    {"value": color, "label": color, "swatch": COLOR_SWATCHES[color]}
                    for color in colors
                ],
            },
        ]
        product, _ = Product.objects.update_or_create(
            store=store,
            ref=ref,
            defaults={
                "section": section,
                "slug": slug,
                "name": name,
                "description": description,
                "price": price,
                "compare_at_price": compare_at,
                "promo": 0,
                "attributes": {},
                "variant_options": variant_options,
                "tags": ["showcase", "fashion"],
                "is_active": True,
                "uses_variants": True,
                "track_inventory": True,
                "is_featured": position <= 4,
                "merchandising_order": position,
                "show_related_products": True,
            },
        )
        product.variants.all().delete()
        for size, color in option_combinations(sizes, colors):
            StockVariant.objects.create(
                store=store,
                product=product,
                attributes={"size": size, "color": color},
                sku=f"{ref}-{size}-{color}".upper(),
                price=price,
                quantity=12,
                reserved=0,
                extra={},
            )
        product.images.all().delete()
        ProductImage.objects.create(
            product=product,
            image=f"tijarago/stores/4/products/lux-showcase/{image_name}",
            media_type="image",
            alt_text=name,
            is_primary=True,
            order=0,
        )
        seeded.append(product)

    for product in seeded:
        product.related_products.set([candidate for candidate in seeded if candidate.pk != product.pk])


def remove_lux_showcase(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    Product.objects.filter(store__slug="lux", ref__startswith=PREFIX).delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0013_product_merchandising_choices")]

    operations = [migrations.RunPython(seed_lux_showcase, remove_lux_showcase)]
