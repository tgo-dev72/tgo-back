"""Curated, read-only visual presets for official theme demonstration tenants."""

from copy import deepcopy


THEME_PRESETS = {
    "basic": [
        {"key": "market-green", "name": "Market Green", "description": "A clear, welcoming general-store identity.", "accent": "#287A52", "surface": "#F4F7F4"},
    ],
    "beauty": [
        {"key": "natural-skincare", "name": "Natural Skincare", "description": "Warm ivory, botanical restraint and soft rose glass.", "accent": "#8B5C55", "surface": "#F7F1EA"},
        {"key": "luxury-cosmetics", "name": "Luxury Cosmetics", "description": "Ink, champagne and editorial product presentation.", "accent": "#A77A38", "surface": "#151311"},
        {"key": "modern-wellness", "name": "Modern Wellness", "description": "Sage, cream and calm contemporary typography.", "accent": "#667862", "surface": "#F1F3EC"},
    ],
    "fashion": [
        {"key": "urban-streetwear", "name": "Urban Streetwear", "description": "Bold contrast for modern drops and everyday layers.", "accent": "#252525", "surface": "#ECEAE5"},
        {"key": "minimal-fashion", "name": "Minimal Fashion", "description": "Quiet neutrals and generous editorial space.", "accent": "#8A765D", "surface": "#F7F5F0"},
        {"key": "luxury-boutique", "name": "Luxury Boutique", "description": "Deep cocoa and antique-gold restraint.", "accent": "#9A7338", "surface": "#211B18"},
    ],
    "electronics": [
        {"key": "modern-tech", "name": "Modern Tech", "description": "Confident blue with clear comparison hierarchy.", "accent": "#1957D2", "surface": "#F3F6FA"},
        {"key": "gaming-accessories", "name": "Gaming Accessories", "description": "Electric violet on a focused dark canvas.", "accent": "#7657E8", "surface": "#11131A"},
        {"key": "minimal-electronics", "name": "Minimal Electronics", "description": "Graphite, silver and precise product detail.", "accent": "#4A5A68", "surface": "#F5F6F7"},
    ],
    "nexa": [
        {"key": "black-gold", "name": "Black & Gold", "description": "The signature NEXA modern-luxury treatment.", "accent": "#B38A36", "surface": "#0E0F11"},
    ],
}


def presets_for_theme(theme_key: str) -> list[dict]:
    return deepcopy(THEME_PRESETS.get(theme_key, []))


def apply_demo_preset(payload: dict, theme_key: str, preset_key: str) -> dict:
    preset = next((item for item in THEME_PRESETS.get(theme_key, []) if item["key"] == preset_key), None)
    if not preset:
        return payload
    result = deepcopy(payload)
    result["palette"] = {
        **result.get("palette", {}),
        "brand": preset["accent"],
        "brandDark": preset["accent"],
        "brandDeep": preset["accent"],
    }
    appearance = result.setdefault("appearance", {})
    colors = appearance.setdefault("colors", {})
    colors.update({"theme_primary": preset["accent"], "page": preset["surface"], "section": preset["surface"]})
    result["demoPreset"] = preset["key"]
    return result
