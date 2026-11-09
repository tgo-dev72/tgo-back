"""Built-in retail-type presets.

These presets are the canonical initial catalog configuration for new stores.
They deliberately describe physical retail only: digital delivery and service
booking need different fulfillment workflows and must not be advertised until
those workflows exist.
"""

EXTRA_FIELD_TYPES = {
    "sku": "text",
    "barcode": "text",
    "product_type": "select",
    "weight": "number",
    "weight_unit": "select",
    "length": "number",
    "width": "number",
    "height": "number",
    "dimension_unit": "select",
    "unit": "select",
    "unit_price": "number",
    "meta_title": "text",
    "meta_description": "text",
    "tags": "string_list",
    "expiry_date": "date",
    "batch_number": "text",
    "download_url": "url",
    "min_quantity": "number",
    "step": "number",
}

EXTRA_FIELD_CHOICES = {
    "product_type": ["standard"],
    "weight_unit": ["g", "kg", "oz", "lb"],
    "dimension_unit": ["mm", "cm", "in"],
    "unit": [
        "piece", "kg", "g", "lb", "oz", "m", "m2", "l", "ml",
        "yd", "ft", "roll", "box", "pair",
    ],
}


def _fields(*keys, required=()):
    required = set(required)
    result = {}
    for key in keys:
        config = {
            "visible": True,
            "required": key in required,
            "type": EXTRA_FIELD_TYPES[key],
        }
        if key in EXTRA_FIELD_CHOICES:
            config["choices"] = EXTRA_FIELD_CHOICES[key]
        result[key] = config
    return result


def _features(*, variants=True, batches=False, weight_based=False):
    return {
        "has_variants": variants,
        "has_batch_tracking": batches,
        "has_digital": False,
        "has_weight_based": weight_based,
    }


COMMON_SEO = ("meta_title", "meta_description", "tags")
COMMON_ID = ("sku", "barcode")
COMMON_SHIPPING = ("weight", "weight_unit")
DIMENSIONS = ("length", "width", "height", "dimension_unit")


RETAIL_TYPE_PRESETS = [
    {
        "key": "clothing",
        "label": "Clothing & Fashion",
        "description": "Apparel, footwear and accessories with size and color variants.",
        "icon": "FaTshirt:fa",
        "sort_order": 10,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece", "pair"],
            "features": _features(),
        },
    },
    {
        "key": "electronics",
        "label": "Electronics",
        "description": "Devices and accessories with model, storage and warranty details.",
        "icon": "FaLaptop:fa",
        "sort_order": 20,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + DIMENSIONS + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece"],
            "features": _features(),
        },
    },
    {
        "key": "food",
        "label": "Food & Grocery",
        "description": "Packaged food, groceries and products sold by weight or volume.",
        "icon": "FaCookieBite:fa",
        "sort_order": 30,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields("sku", "barcode", "expiry_date", "batch_number"),
            "units": ["piece", "box"],
            "features": _features(),
        },
    },
    {
        "key": "jewelry",
        "label": "Jewelry & Accessories",
        "description": "Jewelry with material, stone, size and certification details.",
        "icon": "FaGem:fa",
        "sort_order": 40,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece", "pair"],
            "features": _features(),
        },
    },
    {
        "key": "cosmetics",
        "label": "Cosmetics & Beauty",
        "description": "Beauty and personal-care products with shade and batch details.",
        "icon": "FaFlask:fa",
        "sort_order": 50,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields("sku", "barcode", "expiry_date", "batch_number"),
            "units": ["piece", "g", "ml"],
            "features": _features(),
        },
    },
    {
        "key": "furniture",
        "label": "Furniture & Home",
        "description": "Furniture and home goods with dimensions and material variants.",
        "icon": "FaCouch:fa",
        "sort_order": 60,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + DIMENSIONS + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece", "box"],
            "features": _features(),
        },
    },
    {
        "key": "books",
        "label": "Books & Stationery",
        "description": "Physical books, media and stationery products.",
        "icon": "FaBook:fa",
        "sort_order": 70,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece", "box"],
            "features": _features(),
        },
    },
    {
        "key": "art",
        "label": "Art & Handmade",
        "description": "Original art and handmade products with dimensions and editions.",
        "icon": "FaPaintBrush:fa",
        "sort_order": 80,
        "config": {
            "product_fields": _fields(*(COMMON_ID + DIMENSIONS + COMMON_SEO)),
            "variant_fields": _fields("sku"),
            "units": ["piece"],
            "features": _features(),
        },
    },
    {
        "key": "hardware",
        "label": "Hardware & Tools",
        "description": "Tools, building supplies and materials sold by piece or measure.",
        "icon": "FaWrench:fa",
        "sort_order": 90,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + DIMENSIONS + COMMON_SEO)),
            "variant_fields": _fields("sku", "barcode"),
            "units": ["piece", "box", "roll"],
            "features": _features(),
        },
    },
    {
        "key": "general",
        "label": "General Retail",
        "description": "A flexible catalog for mixed physical products.",
        "icon": "FaBoxOpen:fa",
        "sort_order": 100,
        "config": {
            "product_fields": _fields(*(COMMON_ID + COMMON_SHIPPING + COMMON_SEO)),
            "variant_fields": _fields(*COMMON_ID),
            "units": ["piece", "pair", "box"],
            "features": _features(),
        },
    },
]


RETAIL_TYPE_SECTIONS = {
    "clothing": ["men", "women", "kids", "shoes", "accessories", "hygiene"],
    "electronics": ["electronics"],
    "food": ["food"],
    "jewelry": ["jewelry"],
    "cosmetics": ["cosmetics"],
    "furniture": ["furniture"],
    "books": ["books"],
    "art": ["art"],
    "hardware": ["hardware"],
    "general": ["products"],
}
