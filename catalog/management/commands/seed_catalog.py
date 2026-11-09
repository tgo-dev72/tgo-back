"""Seed a realistic, trilingual-ready catalog for one or all stores.

Seeds CatalogSections + AttributeDefs + Products + StockVariants + SVG
product images (written through the real image storage, so they are served
by this API, not a mock). Product images are generated SVG art so a fresh
storefront renders a full, attractive catalog out of the box.

Usage:
    python manage.py seed_catalog [--store <slug>] [--clear]

`--clear` wipes the store's existing catalog sections/products first so the
command is idempotent.
"""
import random

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from catalog.models import AttributeDef, CatalogSection, Product, ProductImage, StockVariant
from store.models import Store

SECTIONS = [
    {
        "key": "men",
        "label": "Men",
        "icon": "FaTshirt:fa",
        "sort_order": 1,
        "attribute_defs": [
            {"key": "size", "label": "Size", "type": "select", "required": True, "sort_order": 1,
             "options": [{"value": v, "label": v} for v in ["XS", "S", "M", "L", "XL"]]},
            {"key": "color", "label": "Color", "type": "color", "required": True, "sort_order": 2,
             "options": [{"value": v, "label": v} for v in ["Black", "Navy", "Olive", "Wheat"]]},
            {"key": "material", "label": "Material", "type": "select", "required": False, "sort_order": 3,
             "options": [{"value": v, "label": v} for v in ["Cotton", "Linen", "Wool", "Denim"]]},
        ],
    },
    {
        "key": "women",
        "label": "Women",
        "icon": "FaLeaf:fa",
        "sort_order": 2,
        "attribute_defs": [
            {"key": "size", "label": "Size", "type": "select", "required": True, "sort_order": 1,
             "options": [{"value": v, "label": v} for v in ["XS", "S", "M", "L", "XL"]]},
            {"key": "color", "label": "Color", "type": "color", "required": True, "sort_order": 2,
             "options": [{"value": v, "label": v} for v in ["Black", "Bordeaux", "Blush", "Ivory"]]},
            {"key": "fit", "label": "Fit", "type": "select", "required": False, "sort_order": 3,
             "options": [{"value": v, "label": v} for v in ["Slim", "Regular", "Relaxed"]]},
        ],
    },
    {
        "key": "accessories",
        "label": "Accessories",
        "icon": "FaBagShopping:fa6",
        "sort_order": 3,
        "attribute_defs": [
            {"key": "color", "label": "Color", "type": "color", "required": True, "sort_order": 1,
             "options": [{"value": v, "label": v} for v in ["Black", "Tan", "Navy", "Camel"]]},
            {"key": "material", "label": "Material", "type": "select", "required": False, "sort_order": 2,
             "options": [{"value": v, "label": v} for v in ["Leather", "Canvas", "Wool", "Metal"]]},
        ],
    },
    {
        "key": "kids",
        "label": "Kids",
        "icon": "FaHeart:fa",
        "sort_order": 4,
        "attribute_defs": [
            {"key": "size", "label": "Size", "type": "select", "required": True, "sort_order": 1,
             "options": [{"value": v, "label": v} for v in ["2Y", "4Y", "6Y", "8Y", "10Y"]]},
            {"key": "color", "label": "Color", "type": "color", "required": True, "sort_order": 2,
             "options": [{"value": v, "label": v} for v in ["Sky", "Mint", "Coral", "Lemon"]]},
        ],
    },
]

# [section_key, ref, name, description, base_price, promo, loyalty, rating, reviews]
PRODUCTS = [
    ["men", "MN-101", "Garment-Dyed Oversized Tee",
     "Heavyweight cotton, garment-dyed so the colour stays soft after washing. Dropped shoulder, clean rib collar — a fit that's meant to be lived in.",
     180, 0, 10, 4.8, 214],
    ["men", "MN-102", "Selvedge Denim Jacket",
     "Japanese selvedge denim with copper rivets that patina as you wear it. Fades your way — no two jackets age the same.",
     1200, 15, 20, 4.9, 97],
    ["men", "MN-103", "Merino Crewneck",
     "Extra-fine merino knit that breathes in the sun and holds up in a draft. Naturally odour-resistant, so it wears for days between washes.",
     890, 0, 10, 4.7, 158],
    ["men", "MN-104", "Linen Camp Shirt",
     "European linen that crinkles with life and dries in an hour. Coconut buttons and a camp collar — the summer shirt you reach for every day.",
     640, 25, 0, 4.6, 61],
    ["women", "WN-201", "Silk Slip Dress",
     "Bias-cut mulberry silk that skims the body and moves like water. Falls just above the knee — dresses up or down in a gesture.",
     1450, 0, 20, 4.9, 143],
    ["women", "WN-202", "Sand Trench Coat",
     "Double-faced cotton twill with a storm flap and buckled belt. Roomy through the shoulders, built for rain or shine.",
     1890, 10, 30, 4.8, 86],
    ["women", "WN-203", "Pleated Midi Skirt",
     "High-twist crepe that holds its pleats through a full day of sitting, walking and dancing. Midi length, hidden back zip.",
     720, 0, 0, 4.5, 72],
    ["women", "WN-204", "Ribbed Knit Top",
     "Modal-blend rib that hugs without squeezing. Scoop neck, close fit — the quiet base layer that goes with everything.",
     390, 20, 10, 4.7, 129],
    ["accessories", "AC-301", "Weekender Duffel",
     "Full-grain leather that softens with every trip. Brass hardware, a ventilated shoe pocket and room for two days plus a charger.",
     1580, 0, 20, 4.8, 54],
    ["accessories", "AC-302", "Aviator Sunglasses",
     "Polarised lenses and a hand-polished acetate frame, cut, heated and shaped by hand. Hard case and cleaning cloth included.",
     460, 0, 0, 4.6, 188],
    ["accessories", "AC-303", "Wool Beanie",
     "Rib-knit lambswool that doesn't itch and doesn't lose its shape. Six heathered shades — one of them becomes yours.",
     220, 30, 5, 4.4, 95],
    ["accessories", "AC-304", "Leather Belt",
     "Vegetable-tanned leather with a solid brass buckle. Cuts a clean line through denim or tailoring — and darkens beautifully with time.",
     340, 0, 5, 4.7, 108],
    ["kids", "KD-401", "Denim Overalls",
     "Double knees, adjustable straps and denim that softens fast. Room to grow, stains that wash out, knees that survive the playground.",
     480, 0, 10, 4.8, 77],
    ["kids", "KD-402", "Colourblock Raincoat",
     "Colourblock rain shell with snap storm flaps and a hood that actually stays up. Bright enough to spot from across the street.",
     390, 15, 0, 4.3, 41],
    ["kids", "KD-403", "Graphic Hoodie",
     "Brushed-back fleece, soft on the inside and tough on the outside. Drawstring hood and a graphic they'll be proud to wear.",
     320, 0, 5, 4.9, 167],
    ["kids", "KD-404", "Bubble Puffer",
     "Lightweight quilted warmth with a zip front and room for a layer underneath. Packs small and shrugs off the drizzle.",
     560, 10, 10, 4.6, 88],
]

GRADIENTS = [
    ["#232a35", "#0e1116"],
    ["#3b2f2f", "#7a4a2b"],
    ["#1e3a5f", "#0e2a55"],
    ["#3f3a52", "#1a1f33"],
    ["#2c4a3b", "#16301f"],
    ["#4a2f3f", "#2b1624"],
    ["#333a3d", "#14181a"],
    ["#3d3a24", "#1c1a10"],
]


def art_svg(ref: str, index: int) -> str:
    c1, c2 = GRADIENTS[index % len(GRADIENTS)]
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='900' height='1200' viewBox='0 0 900 1200'>"
        f"<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='{c1}'/><stop offset='1' stop-color='{c2}'/>"
        f"</linearGradient></defs>"
        f"<rect width='900' height='1200' fill='url(#g)'/>"
        f"<circle cx='680' cy='220' r='180' fill='rgba(255,255,255,0.07)'/>"
        f"<circle cx='180' cy='980' r='260' fill='rgba(255,255,255,0.05)'/>"
        f"<text x='450' y='600' text-anchor='middle' font-family='system-ui,sans-serif' font-size='64' font-weight='700' fill='rgba(255,255,255,0.85)' letter-spacing='2'>{ref}</text>"
        f"<rect y='1176' width='900' height='24' fill='rgba(255,255,255,0.25)'/>"
        f"</svg>"
    )


SIZE_EXTRA = {"XS": 0, "S": 0, "M": 0, "L": 20, "XL": 30}

class Command(BaseCommand):
    help = "Seed a full catalog (sections, attributes, products, variants, images) for stores."

    def add_arguments(self, parser):
        parser.add_argument("--store", help="Slug of a single store to seed (default: all).")
        parser.add_argument("--clear", action="store_true", help="Wipe the store's catalog before seeding.")

    def handle(self, *args, **options):
        stores = Store.objects.filter(slug=options["store"]) if options["store"] else Store.objects.all()
        if not stores.exists():
            raise CommandError("No store found to seed.")

        for store in stores:
            self._seed_store(store, clear=options["clear"])
        self.stdout.write(self.style.SUCCESS(f"Seeded catalog for {len(stores)} store(s)."))

    def _seed_store(self, store, clear=False):
        if clear:
            store.products.all().delete()
            store.catalog_sections.all().delete()
            store.stock_variants.all().delete()

        rand = random.Random(20260809)
        sections = {}
        for s in SECTIONS:
            section, _ = CatalogSection.objects.update_or_create(
                store=store,
                key=s["key"],
                defaults={"label": s["label"], "icon": s["icon"], "sort_order": s["sort_order"]},
            )
            section.attribute_defs.all().delete()
            for a in s["attribute_defs"]:
                AttributeDef.objects.create(store=store, section=section, **a)
            sections[s["key"]] = section

        for i, (section_key, ref, name, description, price, promo, loyalty, rating, reviews) in enumerate(PRODUCTS):
            section = sections[section_key]
            defs = {d.key: d for d in section.attribute_defs.all()}
            size_def = defs.get("size")
            color_def = defs.get("color")
            other_defs = [d for k, d in defs.items() if k not in ("size", "color")]
            sizes = size_def.options if size_def else []
            colors = color_def.options if color_def else []
            size_values = [o["value"] for o in sizes] or [None]
            color_values = [o["value"] for o in colors] or [None]

            product = Product.objects.create(
                store=store,
                section=section,
                uses_variants=True,
                ref=ref,
                name=name,
                description=description,
                price=price,
                promo=promo,
                loyalty_points=loyalty,
                rating=rating,
                reviews_count=reviews,
            )

            for size in size_values:
                for color in color_values:
                    attributes = {}
                    if size is not None:
                        attributes["size"] = size
                    if color is not None:
                        attributes["color"] = color
                    for d in other_defs:
                        attributes[d.key] = rand.choice([o["value"] for o in d.options])
                    StockVariant.objects.create(
                        store=store,
                        product=product,
                        attributes=attributes,
                        price=price + SIZE_EXTRA.get(size or "", 0),
                        quantity=rand.randint(4, 94),
                        reserved=rand.randint(0, 7),
                    )

            for k in range(2):
                svg = art_svg(ref, (i + k) % len(PRODUCTS))
                filename = f"{store.slug}/{ref}-{k + 1}.svg"
                img = ProductImage.objects.create(
                    product=product,
                    is_primary=k == 0,
                    order=k,
                )
                img.image.save(filename, ContentFile(svg.encode("utf-8")), save=True)

        self.stdout.write(f"  {store.slug}: {len(SECTIONS)} sections, {len(PRODUCTS)} products")
