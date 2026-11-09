"""Add or remove a small, identifiable storefront showcase catalog."""

from itertools import product as option_combinations
from pathlib import Path

from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import CatalogSection, Product, ProductImage, StockVariant
from store.models import Store


SHOWCASE_PREFIX = "SHOWCASE-LUX-"

COLOR_OPTION = {
    "Ivory": "#eee5d5",
    "Black": "#1b1b1b",
    "Indigo": "#263554",
    "Camel": "#b78b5a",
    "Olive": "#555441",
}

PRODUCTS = (
    {
        "ref": f"{SHOWCASE_PREFIX}001",
        "name": "Linen Camp Shirt",
        "description": "Airy linen with a relaxed camp collar, finished for warm days and understated evenings.",
        "price": "640.00",
        "compare_at_price": "790.00",
        "image": "fashion-linen-shirt.png",
        "sizes": ("S", "M", "L", "XL"),
        "colors": ("Ivory", "Black"),
    },
    {
        "ref": f"{SHOWCASE_PREFIX}002",
        "name": "Selvedge Denim Jacket",
        "description": "Structured dark denim with clean copper hardware and a silhouette designed to age beautifully.",
        "price": "1200.00",
        "compare_at_price": None,
        "image": "denim-jacket.png",
        "sizes": ("M", "L", "XL"),
        "colors": ("Indigo", "Black"),
    },
    {
        "ref": f"{SHOWCASE_PREFIX}003",
        "name": "Silk Slip Dress",
        "description": "A fluid satin silhouette with a softly draped neckline and an elegant, effortless fall.",
        "price": "1450.00",
        "compare_at_price": "1690.00",
        "image": "silk-dress.png",
        "sizes": ("XS", "S", "M", "L"),
        "colors": ("Ivory", "Black"),
    },
    {
        "ref": f"{SHOWCASE_PREFIX}004",
        "name": "Sand Trench Coat",
        "description": "A timeless long-line trench in a warm neutral tone, tailored for polished everyday layering.",
        "price": "1890.00",
        "compare_at_price": None,
        "image": "fashion-trench.png",
        "sizes": ("S", "M", "L"),
        "colors": ("Camel", "Black"),
    },
    {
        "ref": f"{SHOWCASE_PREFIX}005",
        "name": "Lightweight Puffer Jacket",
        "description": "Refined warmth without bulk, with a clean stand collar and tonal hardware.",
        "price": "980.00",
        "compare_at_price": "1150.00",
        "image": "fashion-puffer.png",
        "sizes": ("S", "M", "L", "XL"),
        "colors": ("Olive", "Black"),
    },
    {
        "ref": f"{SHOWCASE_PREFIX}006",
        "name": "Signature Graphic Hoodie",
        "description": "A heavyweight cotton hoodie with a restrained hand-drawn motif and a soft brushed interior.",
        "price": "520.00",
        "compare_at_price": None,
        "image": "fashion-hoodie.png",
        "sizes": ("S", "M", "L", "XL"),
        "colors": ("Ivory", "Black"),
    },
)


def _variant_options(item):
    return [
        {"key": "size", "label": "Size", "type": "select", "values": list(item["sizes"])},
        {
            "key": "color",
            "label": "Color",
            "type": "color",
            "values": [
                {"value": color, "label": color, "swatch": COLOR_OPTION[color]}
                for color in item["colors"]
            ],
        },
    ]


class Command(BaseCommand):
    help = "Seed a reversible, variant-rich showcase catalog into one store."

    def add_arguments(self, parser):
        parser.add_argument("--store", default="lux", help="Target store slug (default: lux).")
        parser.add_argument("--media-dir", type=Path, help="Directory containing the showcase images.")
        parser.add_argument("--remove", action="store_true", help="Remove only products created by this command.")

    def handle(self, *args, **options):
        store = Store.objects.filter(slug=options["store"]).first()
        if store is None:
            raise CommandError(f"Store '{options['store']}' does not exist.")

        owned_products = Product.objects.filter(store=store, ref__startswith=SHOWCASE_PREFIX)
        if options["remove"]:
            removed = owned_products.count()
            owned_products.delete()
            self.stdout.write(self.style.SUCCESS(f"Removed {removed} showcase product(s) from {store.slug}."))
            return

        media_dir = options["media_dir"]
        if media_dir is None or not media_dir.is_dir():
            raise CommandError("--media-dir must point to the showcase image directory.")
        missing = [item["image"] for item in PRODUCTS if not (media_dir / item["image"]).is_file()]
        if missing:
            raise CommandError(f"Missing showcase image(s): {', '.join(missing)}")

        section = CatalogSection.objects.filter(store=store, key="products").first()
        if section is None:
            section = CatalogSection.objects.create(
                store=store,
                key="products",
                label="Products",
                icon="FaBoxOpen:fa",
                sort_order=15,
            )

        created_count = 0
        with transaction.atomic():
            for position, item in enumerate(PRODUCTS, start=1):
                product, created = Product.objects.update_or_create(
                    store=store,
                    ref=item["ref"],
                    defaults={
                        "section": section,
                        "name": item["name"],
                        "description": item["description"],
                        "price": item["price"],
                        "compare_at_price": item["compare_at_price"],
                        "promo": 0,
                        "attributes": {},
                        "variant_options": _variant_options(item),
                        "tags": ["showcase", "fashion"],
                        "is_active": True,
                        "uses_variants": True,
                        "track_inventory": True,
                        "is_featured": position <= 4,
                        "merchandising_order": position,
                        "show_related_products": True,
                    },
                )
                product.full_clean()
                product.save()
                created_count += int(created)

                product.variants.all().delete()
                for size, color in option_combinations(item["sizes"], item["colors"]):
                    variant = StockVariant(
                        store=store,
                        product=product,
                        attributes={"size": size, "color": color},
                        sku=f"{item['ref']}-{size}-{color}".upper(),
                        price=item["price"],
                        quantity=12,
                    )
                    variant.full_clean()
                    variant.save()

                if not product.images.exists():
                    path = media_dir / item["image"]
                    image = ProductImage(
                        product=product,
                        alt_text=item["name"],
                        is_primary=True,
                        order=0,
                    )
                    with path.open("rb") as source:
                        image.image.save(f"{store.slug}/{item['ref']}{path.suffix}", File(source), save=False)
                    image.full_clean()
                    image.save()

        total = Product.objects.filter(store=store, ref__startswith=SHOWCASE_PREFIX).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"LUX showcase ready: {total} products ({created_count} new), "
                f"{sum(len(item['sizes']) * len(item['colors']) for item in PRODUCTS)} variants."
            )
        )
