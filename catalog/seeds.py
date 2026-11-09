"""Default catalog seeding for brand-new stores.

Ships with a usable section taxonomy so the dashboard's product form never
opens empty. Each section comes with attribute definitions for variants/stock.
These defaults cover the most common retail verticals.
"""
from django.db import transaction

from .models import AttributeDef, CatalogSection, RetailType
from .retail_presets import RETAIL_TYPE_PRESETS, RETAIL_TYPE_SECTIONS


def _options(values):
    return [{"value": v, "label": v} for v in values]


DEFAULT_SECTIONS = [
    # ── Clothing ───────────────────────────────────────────────────────────
    {
        "key": "men", "label": "Men", "icon": "FaTshirt:fa",
        "attribute_defs": [
            {"key": "size_system", "label": "Sizing system", "type": "select", "level": "product", "required": True, "options": [{"value": "letters", "label": "Letter sizes (XS, S, M…)"}, {"value": "numeric", "label": "Numeric sizes (36, 38, 40…)"}]},
            {"key": "size",     "label": "Size",     "type": "text",   "level": "variant", "required": True},
            {"key": "color",    "label": "Color",    "type": "color",  "level": "variant", "required": True,  "options": _options(["Black", "Navy", "Olive", "Wheat", "White", "Grey"])},
            {"key": "material", "label": "Material", "type": "select", "level": "product", "required": False, "options": _options(["Cotton", "Linen", "Wool", "Denim", "Polyester", "Silk"])},
        ],
    },
    {
        "key": "women", "label": "Women", "icon": "FaBagShopping:fa6",
        "attribute_defs": [
            {"key": "size_system", "label": "Sizing system", "type": "select", "level": "product", "required": True, "options": [{"value": "letters", "label": "Letter sizes (XS, S, M…)"}, {"value": "numeric", "label": "Numeric sizes (36, 38, 40…)"}]},
            {"key": "size",  "label": "Size",  "type": "text",   "level": "variant", "required": True},
            {"key": "color", "label": "Color", "type": "color",  "level": "variant", "required": True,  "options": _options(["Black", "Bordeaux", "Blush", "Ivory", "Nude", "White"])},
            {"key": "fit",   "label": "Fit",   "type": "select", "level": "product", "required": False, "options": _options(["Slim", "Regular", "Relaxed", "Oversized"])},
        ],
    },
    {
        "key": "kids", "label": "Kids", "icon": "FaHeart:fa",
        "attribute_defs": [
            {"key": "size_system", "label": "Sizing system", "type": "select", "level": "product", "required": True, "options": [{"value": "letters", "label": "Letter sizes (XS, S, M…)"}, {"value": "numeric", "label": "Numeric sizes (36, 38, 40…)"}]},
            {"key": "size",  "label": "Size",  "type": "text",   "level": "variant", "required": True},
            {"key": "color", "label": "Color", "type": "color",  "level": "variant", "required": True,  "options": _options(["Sky", "Mint", "Coral", "Lemon", "Pink", "Blue"])},
        ],
    },

    # ── Shoes ──────────────────────────────────────────────────────────────
    {
        "key": "shoes", "label": "Shoes & Sandals", "icon": "FaShoePrints:fa",
        "attribute_defs": [
            {"key": "size",  "label": "Size",  "type": "number", "level": "variant", "required": True,  "options": _options(["36", "37", "38", "39", "40", "41", "42", "43", "44", "45"])},
            {"key": "color", "label": "Color", "type": "color",  "level": "variant", "required": True,  "options": _options(["Black", "Brown", "White", "Tan", "Red", "Navy"])},
        ],
    },

    # ── Accessories ────────────────────────────────────────────────────────
    {
        "key": "accessories", "label": "Accessories", "icon": "FaBoxOpen:fa",
        "attribute_defs": [
            {"key": "color",    "label": "Color",    "type": "color",  "level": "variant", "required": True,  "options": _options(["Black", "Tan", "Navy", "Camel", "Gold", "Silver"])},
            {"key": "material", "label": "Material", "type": "select", "level": "product", "required": False, "options": _options(["Leather", "Canvas", "Wool", "Metal", "Plastic", "Fabric"])},
        ],
    },

    # ── Hygiene ────────────────────────────────────────────────────────────
    {
        "key": "hygiene", "label": "Hygiene & Body", "icon": "FaHeart:fa",
        "attribute_defs": [
            {"key": "size",    "label": "Size",    "type": "select", "level": "variant", "required": False, "options": _options(["Single", "Pack of 3", "Pack of 6", "Travel Size", "Family Size"])},
            {"key": "scent",   "label": "Scent",   "type": "select", "level": "variant", "required": False, "options": _options(["Unscented", "Lavender", "Rose", "Citrus", "Aloe Vera"])},
            {"key": "skin",    "label": "Skin Type", "type": "select", "level": "product", "required": False, "options": _options(["All Types", "Dry", "Oily", "Sensitive", "Combination"])},
        ],
    },

    # ── Electronics ────────────────────────────────────────────────────────
    {
        "key": "electronics", "label": "Electronics", "icon": "FaLaptop:fa",
        "attribute_defs": [
            {"key": "storage", "label": "Storage",    "type": "select", "level": "variant", "required": True,  "options": _options(["16 GB", "32 GB", "64 GB", "128 GB", "256 GB", "512 GB", "1 TB"])},
            {"key": "color",   "label": "Color",      "type": "color",  "level": "variant", "required": True,  "options": _options(["Black", "White", "Silver", "Blue", "Red"])},
            {"key": "brand",   "label": "Brand",      "type": "text",   "level": "product", "required": False},
            {"key": "warranty", "label": "Warranty",  "type": "text",   "level": "product", "required": False},
        ],
    },

    # ── Food & Beverage ────────────────────────────────────────────────────
    {
        "key": "food", "label": "Food & Beverage", "icon": "FaCookieBite:fa",
        "attribute_defs": [
            {"key": "size",  "label": "Size",  "type": "select", "level": "variant", "required": True,  "options": _options(["250g", "500g", "1 kg", "2 kg", "5 kg"])},
            {"key": "flavor", "label": "Flavor", "type": "select", "level": "variant", "required": False, "options": _options(["Original", "Spicy", "Sweet", "Salty", "Vanilla", "Chocolate"])},
        ],
    },

    # ── Jewelry ────────────────────────────────────────────────────────────
    {
        "key": "jewelry", "label": "Jewelry", "icon": "FaGem:fa",
        "attribute_defs": [
            {"key": "metal",     "label": "Metal",     "type": "select",  "level": "variant", "required": True,  "options": _options(["Gold", "Silver", "Platinum", "Rose Gold", "Stainless Steel"])},
            {"key": "stone",     "label": "Stone",     "type": "select",  "level": "variant", "required": False, "options": _options(["None", "Diamond", "Ruby", "Emerald", "Sapphire", "Pearl"])},
            {"key": "size",      "label": "Ring Size", "type": "number",  "level": "variant", "required": False},
            {"key": "certified", "label": "Certified", "type": "boolean", "level": "product", "required": False},
        ],
    },

    # ── Furniture ──────────────────────────────────────────────────────────
    {
        "key": "furniture", "label": "Furniture", "icon": "FaCouch:fa",
        "attribute_defs": [
            {"key": "color",    "label": "Color",    "type": "color",  "level": "variant", "required": True,  "options": _options(["White", "Black", "Walnut", "Oak", "Natural"])},
            {"key": "size",     "label": "Size",     "type": "select", "level": "variant", "required": True,  "options": _options(["Small", "Medium", "Large", "XL"])},
            {"key": "material", "label": "Material", "type": "select", "level": "product", "required": False, "options": _options(["Solid Wood", "MDF", "Metal", "Rattan", "Fabric", "Leather"])},
        ],
    },

    # ── Cosmetics ──────────────────────────────────────────────────────────
    {
        "key": "cosmetics", "label": "Cosmetics", "icon": "FaFlask:fa",
        "attribute_defs": [
            {"key": "shade",   "label": "Shade",   "type": "select", "level": "variant", "required": True,  "options": _options(["Light", "Medium", "Tan", "Deep", "Universal"])},
            {"key": "finish",  "label": "Finish",  "type": "select", "level": "variant", "required": False, "options": _options(["Matte", "Glossy", "Satin", "Natural", "Shimmer"])},
            {"key": "skin",    "label": "Skin Type", "type": "select", "level": "product", "required": False, "options": _options(["All Types", "Dry", "Oily", "Sensitive"])},
        ],
    },

    # ── Books & Media ──────────────────────────────────────────────────────
    {
        "key": "books", "label": "Books & Media", "icon": "FaBook:fa",
        "attribute_defs": [
            {"key": "format",  "label": "Format",  "type": "select", "level": "variant", "required": True,  "options": _options(["Hardcover", "Paperback"])},
            {"key": "lang",    "label": "Language", "type": "select", "level": "product", "required": False, "options": _options(["Arabic", "English", "French", "Spanish"])},
        ],
    },

    # ── Art & Handmade ─────────────────────────────────────────────────────
    {
        "key": "art", "label": "Art & Handmade", "icon": "FaPaintBrush:fa",
        "attribute_defs": [
            {"key": "medium", "label": "Medium",  "type": "select",  "level": "product", "required": False, "options": _options(["Oil", "Acrylic", "Watercolor", "Mixed Media", "Photography", "Ceramic"])},
            {"key": "artist", "label": "Artist",  "type": "text",    "level": "product", "required": False},
            {"key": "signed", "label": "Signed",  "type": "boolean", "level": "product", "required": False},
        ],
    },

    # ── Hardware & Tools ───────────────────────────────────────────────────
    {
        "key": "hardware", "label": "Hardware & Tools", "icon": "FaWrench:fa",
        "attribute_defs": [
            {"key": "size",  "label": "Size",  "type": "select", "level": "variant", "required": True,  "options": _options(["XS", "S", "M", "L", "XL"])},
            {"key": "brand", "label": "Brand", "type": "text",   "level": "product", "required": False},
        ],
    },
    # ── General retail ─────────────────────────────────────────────────────
    {
        "key": "products", "label": "Products", "icon": "FaBoxOpen:fa",
        "attribute_defs": [
            {"key": "option", "label": "Option", "type": "text", "level": "variant", "required": False},
            {"key": "brand", "label": "Brand", "type": "text", "level": "product", "required": False},
        ],
    },
]


def ensure_retail_types(*, using="default"):
    """Create/update the built-in retail choices and return their count."""
    with transaction.atomic(using=using):
        for preset in RETAIL_TYPE_PRESETS:
            values = {key: value for key, value in preset.items() if key != "key"}
            retail_type = RetailType.objects.using(using).filter(key=preset["key"]).first()
            if retail_type is None:
                retail_type = RetailType(key=preset["key"])
            for field, value in values.items():
                setattr(retail_type, field, value)
            # Presets are executable configuration. Refuse to persist an
            # invalid schema instead of letting future stores inherit it.
            retail_type.full_clean()
            retail_type.save(using=using)
    return len(RETAIL_TYPE_PRESETS)


@transaction.atomic
def ensure_default_sections(store, force=False):
    """Create the default sections for a store unless it already has any.

    If the store has a retail_type, only sections relevant to that type
    are seeded.  Falls back to ALL sections when no retail_type is set.
    """
    existing = set(store.catalog_sections.values_list("key", flat=True))
    if existing and not force:
        return 0

    rt_key = store.retail_type.key if store.retail_type else None
    allowed = set(RETAIL_TYPE_SECTIONS.get(rt_key, [])) if rt_key else None  # None = all

    created = 0
    for i, spec in enumerate(DEFAULT_SECTIONS, start=1):
        if allowed is not None and spec["key"] not in allowed:
            continue
        if spec["key"] in existing:
            continue
        section = CatalogSection.objects.create(
            store=store, key=spec["key"], label=spec["label"],
            icon=spec["icon"], sort_order=i,
        )
        for order, d in enumerate(spec["attribute_defs"], start=1):
            AttributeDef.objects.create(
                store=store, section=section, sort_order=order,
                **{k: v for k, v in d.items() if k != "options"},
                options=d.get("options", []),
            )
        created += 1
    return created
