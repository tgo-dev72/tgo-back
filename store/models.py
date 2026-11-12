import re
import uuid
from copy import deepcopy

from django.conf import settings
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from phonenumber_field.modelfields import PhoneNumberField

HEX_COLOR_RE = r'^#[0-9a-fA-F]{6}$'
SOCIAL_CHANNELS = frozenset({
    "facebook", "instagram", "linkedin", "pinterest", "tiktok", "x", "youtube",
})
STOREFRONT_LOCALES = frozenset({"en", "fr", "ar"})
STOREFRONT_SECTION_TYPES = frozenset({
    "announcement", "hero", "slideshow", "promotional_banner", "collections", "products", "image_text", "newsletter", "theme_feature",
    "product_main", "product_assurances", "related_products",
})
HOMEPAGE_PRODUCT_SOURCES = frozenset({"featured", "newest", "discounted", "top_rated"})
SYSTEM_STOREFRONT_PAGE_KEYS = frozenset({"home", "catalog", "product", "account", "login"})
PRODUCT_STOREFRONT_SECTION_TYPES = frozenset({"product_main", "product_assurances", "related_products"})
STOREFRONT_APPEARANCE_CHOICES = {
    "body_font": frozenset({"system", "modern", "humanist", "editorial", "arabic"}),
    "heading_font": frozenset({"system", "modern", "humanist", "editorial", "arabic"}),
    "base_size": frozenset({"small", "medium", "large"}),
    "heading_scale": frozenset({"compact", "medium", "large"}),
    "header_layout": frozenset({"theme", "logo_start", "logo_center"}),
    "header_sticky": frozenset({"none", "always"}),
    "header_width": frozenset({"contained", "full"}),
    "header_logo_size": frozenset({"compact", "standard", "large"}),
    "header_nav_alignment": frozenset({"start", "center"}),
    "button_shape": frozenset({"square", "rounded", "pill"}),
    "card_shape": frozenset({"square", "rounded", "soft"}),
    "card_padding": frozenset({"none", "compact", "comfortable"}),
    "card_alignment": frozenset({"start", "center"}),
    "card_hover": frozenset({"none", "shadow", "lift"}),
    "content_width": frozenset({"compact", "standard", "wide"}),
}


def localized_text(en="", fr="", ar=""):
    return {"en": en, "fr": fr, "ar": ar}


def default_storefront_appearance():
    """Stable, provider-neutral theme settings consumed by every design."""
    return {
        "schema_version": 1,
        "typography": {
            "body_font": "system",
            "heading_font": "system",
            "base_size": "medium",
            "heading_scale": "medium",
        },
        "header": {
            "layout": "theme",
            "sticky": "always",
            "width": "contained",
            "logo_size": "standard",
            "nav_alignment": "center",
            "show_search": True,
            "show_account": True,
            "show_language": True,
            "show_cart": True,
            "show_categories": False,
            "show_about": True,
            "show_contact": True,
        },
        "style": {
            "button_shape": "rounded",
            "card_shape": "rounded",
            "card_padding": "comfortable",
            "card_alignment": "center",
            "card_hover": "shadow",
            "content_width": "standard",
        },
        "colors": {
            "theme_primary": "",
            "theme_secondary": "",
            "theme_muted": "",
            "page": "",
            "header": "",
            "header_text": "",
            "navigation": "",
            "navigation_text": "",
            "menu_dropdown": "",
            "announcement": "",
            "hero": "",
            "section": "",
            "card": "",
            "discount_badge": "",
            "discount_badge_text": "",
            "newsletter": "",
            "footer": "",
        },
        "footer": {
            "show_contact": True,
            "show_social_links": True,
            "copyright": localized_text(),
        },
    }


def normalize_storefront_appearance(value):
    """Validate and merge the public appearance contract with safe defaults."""
    if not isinstance(value, dict):
        raise ValidationError("Appearance settings must be an object.")
    allowed_groups = {"schema_version", "typography", "header", "style", "colors", "footer"}
    if set(value) - allowed_groups:
        raise ValidationError("Appearance settings contain unsupported groups.")
    if value.get("schema_version", 1) != 1:
        raise ValidationError("This appearance settings version is not supported.")

    result = default_storefront_appearance()
    group_fields = {
        "typography": {"body_font", "heading_font", "base_size", "heading_scale"},
        "header": {"layout", "sticky", "width", "logo_size", "nav_alignment", "show_search", "show_account", "show_language", "show_cart", "show_categories", "show_about", "show_contact"},
        "style": {"button_shape", "card_shape", "card_padding", "card_alignment", "card_hover", "content_width"},
        "colors": {"theme_primary", "theme_secondary", "theme_muted", "page", "header", "header_text", "navigation", "navigation_text", "menu_dropdown", "announcement", "hero", "section", "card", "discount_badge", "discount_badge_text", "newsletter", "footer"},
        "footer": {"show_contact", "show_social_links", "copyright"},
    }
    for group, allowed in group_fields.items():
        supplied = value.get(group, {})
        if not isinstance(supplied, dict) or set(supplied) - allowed:
            raise ValidationError(f"{group.title()} appearance settings are invalid.")
        result[group].update(deepcopy(supplied))

    choice_paths = {
        ("typography", "body_font"): "body_font",
        ("typography", "heading_font"): "heading_font",
        ("typography", "base_size"): "base_size",
        ("typography", "heading_scale"): "heading_scale",
        ("header", "layout"): "header_layout",
        ("header", "sticky"): "header_sticky",
        ("header", "width"): "header_width",
        ("header", "logo_size"): "header_logo_size",
        ("header", "nav_alignment"): "header_nav_alignment",
        ("style", "button_shape"): "button_shape",
        ("style", "card_shape"): "card_shape",
        ("style", "card_padding"): "card_padding",
        ("style", "card_alignment"): "card_alignment",
        ("style", "card_hover"): "card_hover",
        ("style", "content_width"): "content_width",
    }
    for (group, field), choices_key in choice_paths.items():
        if result[group][field] not in STOREFRONT_APPEARANCE_CHOICES[choices_key]:
            raise ValidationError(f"Choose a supported value for {group}.{field}.")
    for field, color in result["colors"].items():
        if not isinstance(color, str) or (color and not re.fullmatch(HEX_COLOR_RE, color)):
            raise ValidationError(f"colors.{field} must be empty or a 6-digit hex color.")
        result["colors"][field] = color.upper()
    for group, field in (("header", "show_search"), ("header", "show_account"), ("header", "show_language"), ("header", "show_cart"), ("header", "show_categories"), ("header", "show_about"), ("header", "show_contact"), ("footer", "show_contact"), ("footer", "show_social_links")):
        if not isinstance(result[group][field], bool):
            raise ValidationError(f"{group}.{field} must be true or false.")
    copyright_value = result["footer"]["copyright"]
    if not isinstance(copyright_value, dict) or set(copyright_value) - STOREFRONT_LOCALES:
        raise ValidationError("Footer copyright must contain only EN, FR and AR text.")
    result["footer"]["copyright"] = {
        locale: str(copyright_value.get(locale, "")).strip()[:160]
        for locale in ("en", "fr", "ar")
    }
    return result


def validate_storefront_appearance(value):
    normalize_storefront_appearance(value)


def default_homepage_sections():
    """A complete, honest starter page: useful without inventing merchant offers."""
    return [
        {
            "id": "announcement",
            "type": "announcement",
            "enabled": False,
            "settings": {
                "text": localized_text(),
                "link_label": localized_text(),
                "link_url": "",
            },
        },
        {
            "id": "hero",
            "type": "hero",
            "enabled": True,
            "settings": {
                "eyebrow": localized_text("Welcome", "Bienvenue", "مرحباً"),
                "title": localized_text(),
                "subtitle": localized_text(
                    "Discover our products, selected for you.",
                    "Découvrez nos produits, sélectionnés pour vous.",
                    "اكتشف منتجاتنا المختارة من أجلك.",
                ),
                "cta_label": localized_text("Browse products", "Voir les produits", "تصفح المنتجات"),
                "cta_url": "#products",
                "image_url": "",
                "show_overlay": True,
                "show_text": True,
                "show_button": True,
            },
        },
        {
            "id": "collections",
            "type": "collections",
            "enabled": True,
            "settings": {
                "title": localized_text("Shop by category", "Acheter par catégorie", "تسوق حسب الفئة"),
                "limit": 6,
            },
        },
        {
            "id": "products",
            "type": "products",
            "enabled": True,
            "settings": {
                "title": localized_text("Featured products", "Produits à la une", "منتجات مختارة"),
                "source": "featured",
                "limit": 8,
                "layout": "grid",
            },
        },
        {
            "id": "story",
            "type": "image_text",
            "enabled": False,
            "settings": {
                "title": localized_text("Our story", "Notre histoire", "قصتنا"),
                "body": localized_text(),
                "cta_label": localized_text(),
                "cta_url": "",
                "image_url": "",
                "alignment": "image_first",
            },
        },
        {
            "id": "newsletter",
            "type": "newsletter",
            "enabled": True,
            "settings": {
                "title": localized_text("Stay in touch", "Restons en contact", "ابق على تواصل"),
                "body": localized_text(
                    "Receive new arrivals and store news.",
                    "Recevez les nouveautés et les actualités de la boutique.",
                    "توصل بالجديد وأخبار المتجر.",
                ),
            },
        },
    ]


def homepage_sections_for_config(config):
    sections = default_homepage_sections()
    products = next(section for section in sections if section["type"] == "products")
    products["settings"]["source"] = config.homepage_product_source
    products["settings"]["limit"] = config.homepage_product_limit
    return sections


def default_content_sections(title, subtitle, body=""):
    return [
        {
            "id": "hero",
            "type": "hero",
            "enabled": True,
            "settings": {
                "eyebrow": localized_text(),
                "title": deepcopy(title),
                "subtitle": deepcopy(subtitle),
                "cta_label": localized_text(),
                "cta_url": "",
                "image_url": "",
                "show_overlay": True,
                "show_text": True,
                "show_button": True,
            },
        },
        {
            "id": "content",
            "type": "image_text",
            "enabled": True,
            "settings": {
                "title": deepcopy(title),
                "body": deepcopy(body),
                "cta_label": localized_text(),
                "cta_url": "",
                "image_url": "",
                "alignment": "image_first",
            },
        },
    ]


def default_product_sections():
    """Safe product template shared by every design and retail type."""
    return [
        {
            "id": "product-main",
            "type": "product_main",
            "enabled": True,
            "settings": {
                "show_reference": True,
                "show_rating": True,
                "show_description": True,
                "show_stock": True,
                "show_quantity": True,
                "show_facts": True,
            },
        },
        {
            "id": "product-assurances",
            "type": "product_assurances",
            "enabled": True,
            "settings": {
                "first": localized_text(
                    "Help from the store team",
                    "Aide de l’équipe de la boutique",
                    "مساعدة من فريق المتجر",
                ),
                "second": localized_text(
                    "Straightforward order support",
                    "Accompagnement clair pour vos commandes",
                    "دعم واضح ومباشر للطلبات",
                ),
            },
        },
        {
            "id": "related-products",
            "type": "related_products",
            "enabled": True,
            "settings": {
                "title": localized_text(
                    "Explore related products",
                    "Découvrez des produits similaires",
                    "اكتشف منتجات ذات صلة",
                ),
                "limit": 4,
            },
        },
    ]


def default_storefront_page_specs(config=None):
    """Protected commerce pages plus editable informational starter pages."""
    home_sections = homepage_sections_for_config(config) if config else default_homepage_sections()
    catalog_title = localized_text("Products", "Produits", "المنتجات")
    catalog_sections = default_content_sections(
        catalog_title,
        localized_text(
            "Explore everything available in our store.",
            "Découvrez tout ce qui est disponible dans notre boutique.",
            "اكتشف كل ما هو متوفر في متجرنا.",
        ),
    )[:1]
    catalog_sections.append({
        "id": "products",
        "type": "products",
        "enabled": True,
        "settings": {"title": deepcopy(catalog_title), "source": "newest", "limit": 24, "layout": "grid"},
    })
    definitions = [
        ("home", "system", "", localized_text("Homepage", "Accueil", "الرئيسية"), True, home_sections),
        ("catalog", "system", "products", catalog_title, True, catalog_sections),
        ("product", "system", "product-template", localized_text("Product page", "Page produit", "صفحة المنتج"), False, default_product_sections()),
        ("about", "content", "about", localized_text("About us", "À propos", "عن المتجر"), True, None),
        ("contact", "content", "contact", localized_text("Contact us", "Contact", "اتصل بنا"), True, None),
        ("faq", "content", "faq", localized_text("Frequently asked questions", "Questions fréquentes", "الأسئلة المتكررة"), False, None),
        ("shipping", "policy", "shipping", localized_text("Shipping and delivery", "Livraison", "الشحن والتسليم"), False, None),
        ("returns", "policy", "returns", localized_text("Returns and refunds", "Retours et remboursements", "الاستبدال والاسترجاع"), False, None),
        ("payment", "policy", "payment", localized_text("Payment methods", "Modes de paiement", "طرق الدفع"), False, None),
        ("privacy", "policy", "privacy", localized_text("Privacy policy", "Politique de confidentialité", "سياسة الخصوصية"), False, None),
        ("terms", "policy", "terms", localized_text("Terms of service", "Conditions d’utilisation", "شروط الاستخدام"), False, None),
        ("account", "system", "account", localized_text("Customer account", "Compte client", "حساب العميل"), False, None),
        ("login", "system", "login", localized_text("Customer sign in", "Connexion client", "دخول العميل"), False, None),
    ]
    specs = []
    for sort_order, (key, page_type, route, title, in_navigation, sections) in enumerate(definitions):
        if sections is None:
            sections = default_content_sections(
                title,
                localized_text(),
                localized_text(),
            )
            # Account routes render the real authentication/account component
            # below this editable introduction. They do not need an empty
            # image-and-text placeholder in a newly created store.
            if key in {"account", "login"}:
                sections = sections[:1]
        specs.append({
            "page_key": key,
            "page_type": page_type,
            "route": route,
            "title": title,
            "is_visible": True,
            "show_in_navigation": in_navigation,
            "sort_order": sort_order,
            "draft_sections": sections,
            "published_sections": deepcopy(sections),
        })
    return specs


def _validate_localized_text(value, field, *, required=False, max_length=500):
    if not isinstance(value, dict) or set(value) - STOREFRONT_LOCALES:
        raise ValidationError(f"{field} must contain only en, fr and ar text.")
    if any(not isinstance(text, str) or len(text) > max_length for text in value.values()):
        raise ValidationError(f"{field} contains invalid or overly long text.")
    if required and not any(text.strip() for text in value.values()):
        raise ValidationError(f"{field} cannot be empty in every language.")


def _validate_localized_rich_text(value, field):
    """Allow bounded, sanitized HTML in merchant-authored descriptive copy."""
    from catalog.rich_text import sanitize_product_description

    _validate_localized_text(value, field, max_length=6000)
    for text in value.values():
        if re.search(r"</?[a-z][^>]*>", text, re.IGNORECASE) and sanitize_product_description(text) != text:
            raise ValidationError(f"{field} contains unsupported formatting.")


def _validate_storefront_url(value, field, *, image=False):
    if value == "":
        return
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a URL.")
    if not image and (value.startswith("#") or (value.startswith("/") and not value.startswith("//"))) and "\\" not in value:
        return
    try:
        URLValidator(schemes=["https"])(value)
    except ValidationError as exc:
        raise ValidationError(f"{field} must use HTTPS or link to a page in this store.") from exc


def validate_storefront_sections(value):
    """Validate the bounded document consumed by every storefront renderer."""
    if not isinstance(value, list):
        raise ValidationError("Page sections must be a list.")
    if len(value) > 25:
        raise ValidationError("A page can contain at most 25 sections.")
    seen_ids = set()
    schemas = {
        "announcement": {"text", "link_label", "link_url"},
        "hero": {"eyebrow", "title", "subtitle", "cta_label", "cta_url", "image_url", "show_overlay", "show_text", "show_button"},
        "promotional_banner": {"title", "body", "cta_label", "cta_url", "image_url", "width", "height"},
        "collections": {"title", "limit"},
        "products": {"title", "source", "limit", "layout"},
        "image_text": {"title", "body", "cta_label", "cta_url", "image_url", "alignment"},
        "newsletter": {"title", "body"},
        "slideshow": {"slides", "autoplay", "interval"},
        "product_main": {"show_reference", "show_rating", "show_description", "show_stock", "show_quantity", "show_facts"},
        "product_assurances": {"first", "second"},
        "related_products": {"title", "limit"},
        "theme_feature": {"title", "body", "kind", "limit"},
    }
    localized_fields = {"text", "link_label", "eyebrow", "title", "subtitle", "cta_label", "body", "first", "second"}
    for index, section in enumerate(value):
        label = f"Section {index + 1}"
        if not isinstance(section, dict):
            raise ValidationError(f"{label} must be an object.")
        if set(section) != {"id", "type", "enabled", "settings"}:
            raise ValidationError(f"{label} has unsupported fields.")
        section_id = section["id"]
        if not isinstance(section_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", section_id):
            raise ValidationError(f"{label} has an invalid identifier.")
        if section_id in seen_ids:
            raise ValidationError("Every page section needs a unique identifier.")
        seen_ids.add(section_id)
        section_type = section["type"]
        if section_type not in STOREFRONT_SECTION_TYPES:
            raise ValidationError(f"{label} has an unsupported type.")
        if not isinstance(section["enabled"], bool):
            raise ValidationError(f"{label} visibility must be true or false.")
        settings_value = section["settings"]
        announcement_items = section_type == "announcement" and isinstance(settings_value, dict) and set(settings_value) == {"items"}
        legacy_hero = section_type == "hero" and isinstance(settings_value, dict) and set(settings_value) == {
            "eyebrow", "title", "subtitle", "cta_label", "cta_url", "image_url",
        }
        if not isinstance(settings_value, dict) or (set(settings_value) != schemas[section_type] and not announcement_items and not legacy_hero):
            raise ValidationError(f"{label} settings do not match its section type.")
        if announcement_items:
            items = settings_value["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= 8:
                raise ValidationError(f"{label} needs between 1 and 8 messages.")
            item_ids = set()
            for item in items:
                if not isinstance(item, dict) or set(item) != {"id", "text", "link_label", "link_url"}:
                    raise ValidationError(f"{label} contains invalid message fields.")
                item_id = item["id"]
                if not isinstance(item_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item_id) or item_id in item_ids:
                    raise ValidationError(f"{label} message identifiers must be valid and unique.")
                item_ids.add(item_id)
                _validate_localized_text(item["text"], f"{label} message text", required=True, max_length=180)
                _validate_localized_text(item["link_label"], f"{label} message link label", max_length=180)
                _validate_storefront_url(item["link_url"], f"{label} message link")
        for field in localized_fields & set(settings_value):
            if field in {"body", "subtitle"}:
                _validate_localized_rich_text(settings_value[field], f"{label} {field}")
            else:
                _validate_localized_text(
                    settings_value[field],
                    f"{label} {field}",
                    required=field == "title" and section_type in {"collections", "products"},
                    max_length=180,
                )
        for field in {"link_url", "cta_url", "image_url"} & set(settings_value):
            _validate_storefront_url(settings_value[field], f"{label} {field}", image=field == "image_url")
        if section_type == "slideshow":
            slides = settings_value["slides"]
            if not isinstance(slides, list) or not 1 <= len(slides) <= 6:
                raise ValidationError(f"{label} needs between 1 and 6 slides.")
            if not isinstance(settings_value["autoplay"], bool):
                raise ValidationError(f"{label} autoplay must be true or false.")
            interval = settings_value["interval"]
            if isinstance(interval, bool) or not isinstance(interval, int) or not 3 <= interval <= 15:
                raise ValidationError(f"{label} interval must be between 3 and 15 seconds.")
            slide_ids = set()
            for slide in slides:
                fields = {"id", "title", "body", "cta_label", "cta_url", "image_url", "mobile_image_url", "alt"}
                if not isinstance(slide, dict) or set(slide) != fields:
                    raise ValidationError(f"{label} contains invalid slide fields.")
                slide_id = slide["id"]
                if not isinstance(slide_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slide_id) or slide_id in slide_ids:
                    raise ValidationError(f"{label} slide identifiers must be valid and unique.")
                slide_ids.add(slide_id)
                for field in ("title", "body", "cta_label", "alt"):
                    if field == "body":
                        _validate_localized_rich_text(slide[field], f"{label} slide {field}")
                    else:
                        _validate_localized_text(slide[field], f"{label} slide {field}", max_length=180)
                for field in ("image_url", "mobile_image_url", "cta_url"):
                    _validate_storefront_url(slide[field], f"{label} slide {field}", image=field != "cta_url")
        if section_type in {"collections", "products", "related_products", "theme_feature"}:
            limit = settings_value["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 24:
                raise ValidationError(f"{label} limit must be between 1 and 24.")
        if section_type == "product_main" and any(
            not isinstance(settings_value[field], bool) for field in schemas[section_type]
        ):
            raise ValidationError(f"{label} display settings must be true or false.")
        if section_type == "hero" and any(
            not isinstance(settings_value[field], bool)
            for field in ("show_overlay", "show_text", "show_button")
            if field in settings_value
        ):
            raise ValidationError(f"{label} banner display settings must be true or false.")
        if section_type == "products":
            if settings_value["source"] not in HOMEPAGE_PRODUCT_SOURCES:
                raise ValidationError(f"{label} has an unsupported product selection.")
            if settings_value["layout"] not in {"grid", "slider"}:
                raise ValidationError(f"{label} layout must be grid or slider.")
        if section_type == "theme_feature" and settings_value["kind"] not in {"beauty", "fashion", "electronics", "atelier", "nexa"}:
            raise ValidationError(f"{label} has an unsupported theme feature.")
        if section_type == "image_text" and settings_value["alignment"] not in {"image_first", "text_first"}:
            raise ValidationError(f"{label} has an unsupported alignment.")
        if section_type == "promotional_banner":
            if settings_value["width"] not in {"full", "contained"}:
                raise ValidationError(f"{label} has an unsupported banner width.")
            if settings_value["height"] not in {"compact", "standard", "tall"}:
                raise ValidationError(f"{label} has an unsupported banner height.")


def validate_storefront_page_sections(page_key, value):
    """Keep commerce-only blocks on their intended protected template."""
    validate_storefront_sections(value)
    types = [section["type"] for section in value]
    if page_key != "home" and "theme_feature" in types:
        raise ValidationError("Theme features can only be used on the homepage.")
    if page_key == "product":
        unsupported = set(types) - PRODUCT_STOREFRONT_SECTION_TYPES
        if unsupported:
            raise ValidationError("The product page only accepts product-specific sections.")
        product_main = [section for section in value if section["type"] == "product_main"]
        if len(product_main) != 1 or not product_main[0]["enabled"]:
            raise ValidationError("The essential product purchase section must remain available.")
    elif set(types) & PRODUCT_STOREFRONT_SECTION_TYPES:
        raise ValidationError("Product-specific sections can only be used on the product page.")


def validate_social_links(value):
    """Validate the public social profile map stored on a store config."""
    if not isinstance(value, dict):
        raise ValidationError("Social links must be an object.")
    unknown = set(value) - SOCIAL_CHANNELS
    if unknown:
        raise ValidationError(f"Unsupported social channels: {', '.join(sorted(unknown))}.")
    validate_url = URLValidator(schemes=["https"])
    errors = []
    for channel, url in value.items():
        if not isinstance(url, str) or not url.strip():
            errors.append(f"{channel} must contain a public profile URL.")
            continue
        try:
            validate_url(url.strip())
        except ValidationError:
            errors.append(f"{channel} must contain a valid HTTPS profile URL.")
    if errors:
        raise ValidationError(errors)


def default_payment_provider():
    return settings.STOREFRONT_DEFAULT_PAYMENT_GATEWAY


def storefront_asset_path(instance, filename):
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "image"
    return f"storefront-assets/{instance.store.slug}/{uuid.uuid4().hex}.{extension}"


def store_logo_path(instance, filename):
    """Use tenant-scoped immutable names to avoid CDN/browser stale-logo caches."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "image"
    return f"store-logos/{instance.slug}/{uuid.uuid4().hex}.{extension}"


class Store(models.Model):
    """A merchant's shop — the multi-tenant boundary for the commerce API."""
    slug = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    logo = models.ImageField(upload_to=store_logo_path, blank=True, null=True)
    retail_type = models.ForeignKey(
        "catalog.RetailType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="stores", help_text="Informational preset used for initial catalog defaults.",
    )
    catalog_config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_suspended = models.BooleanField(
        default=False,
        help_text="Platform safety switch. Suspended stores remain manageable but are not search-indexable.",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.slug})"


class StoreConfig(models.Model):
    """Per-store settings (1:1 with Store, auto-created).

    Merchants bring their own accounts, not yours: payment keys, from-email,
    currency, and commerce toggles live here — never in global env."""
    store = models.OneToOneField(Store, on_delete=models.CASCADE, related_name="config")
    class Plans(models.TextChoices):
        FREE = "free", "Free"
        PREMIUM = "premium", "Premium"

    plan = models.CharField(
        max_length=20,
        choices=Plans.choices,
        default=Plans.FREE,
        help_text="Platform subscription tier. Existing and newly created stores start on Free.",
    )
    currency = models.CharField(max_length=8, default="MAD", help_text="ISO 4217, e.g. MAD, EUR, USD.")
    default_locale = models.CharField(
        max_length=2,
        choices=(("ar", "Arabic"), ("fr", "French"), ("en", "English")),
        default="ar",
        help_text="Initial storefront language for customers without a saved preference.",
    )
    payment_provider = models.CharField(
        max_length=40,
        default=default_payment_provider,
        help_text="Payment gateway adapter code. Existing attempts retain their original provider.",
    )
    payment_keys = models.JSONField(
        default=dict, blank=True,
        help_text="Per-provider credential vault interpreted only by payment adapters.",
    )
    cod_enabled = models.BooleanField(
        default=True,
        help_text="Allow customers to place cash-on-delivery orders.",
    )
    from_email = models.EmailField(blank=True, default="", help_text="Sender address for store emails.")
    email_forward_pending = models.EmailField(blank=True, default="", help_text="Destination awaiting Cloudflare verification.")
    email_forward_active = models.EmailField(blank=True, default="", help_text="Verified forwarding destination.")
    email_forward_rule_id = models.CharField(max_length=32, blank=True, default="")
    brand_color = models.CharField(max_length=7, default="#1F8A8C", help_text="Hex brand color, e.g. #1F8A8C.")
    remove_platform_branding = models.BooleanField(
        default=False,
        help_text="Merchant preference; effective only while the store owner has the branding entitlement.",
    )
    loyalty_enabled = models.BooleanField(default=True, help_text="Award loyalty points on confirmed orders.")
    shipping_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))], help_text="Flat shipping fee.")
    free_shipping_enabled = models.BooleanField(default=False)
    free_shipping_threshold = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0.00"))], help_text="Applies only when the discounted merchandise subtotal is strictly greater than this amount.")
    free_shipping_currency = models.CharField(max_length=8, blank=True, default="")
    invoice_enabled = models.BooleanField(default=False)
    invoice_legal_name = models.CharField(max_length=200, blank=True, default="")
    invoice_billing_address = models.TextField(blank=True, default="")
    invoice_country = models.CharField(max_length=100, blank=True, default="")
    invoice_contact_email = models.EmailField(blank=True, default="")
    invoice_identifiers = models.JSONField(default=dict, blank=True)
    invoice_issuer_scope = models.CharField(max_length=64, blank=True, default="", help_text="Approved legal issuer scope shared by stores that use one sequence.")
    invoice_series_prefix = models.CharField(max_length=20, blank=True, default="FAC")
    invoice_tax_regime = models.CharField(max_length=20, choices=(("review", "Requires review"), ("exempt", "Tax exempt"), ("product", "Product tax rates")), default="review")
    invoice_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(Decimal("100.00"))], help_text="Merchant-declared store-wide tax percentage. Zero means tax exempt.")
    invoice_prices_include_tax = models.BooleanField(default=True)
    invoice_shipping_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(Decimal("0.00"))])
    invoice_tax_wording = models.CharField(max_length=255, blank=True, default="")
    # ── storefront (Option A: one generic deployment, store resolved at runtime) ──
    # These fields are what the generic storefront app reads from
    # /api/storefront/config/. Stored on the store (not on a per-store code
    # copy) so a store can be created and served without any deploy step.
    design = models.CharField(
        max_length=50, default="basic",
        help_text="Business-family storefront key from the design-stock manifest.",
    )
    contact_email = models.EmailField(
        blank=True, default="",
        help_text="Public support email shown in the storefront footer.",
    )
    support_phone = PhoneNumberField(
        blank=True, default="", region=None,
        help_text="Public support number in international format.",
    )
    whatsapp_phone = PhoneNumberField(
        blank=True, default="", region=None,
        help_text="Optional public WhatsApp number in international format.",
    )
    social_links = models.JSONField(
        default=dict, blank=True, validators=[validate_social_links],
        help_text="Public HTTPS social profile links keyed by channel.",
    )
    appearance = models.JSONField(
        default=default_storefront_appearance,
        validators=[validate_storefront_appearance],
        help_text="Versioned provider-neutral typography, header, surface, and footer settings.",
    )
    class HomepageProductSources(models.TextChoices):
        FEATURED = "featured", "Merchant featured products"
        NEWEST = "newest", "Newest products"
        DISCOUNTED = "discounted", "Products with an active discount"
        TOP_RATED = "top_rated", "Top-rated products"

    homepage_product_source = models.CharField(
        max_length=20,
        choices=HomepageProductSources.choices,
        default=HomepageProductSources.FEATURED,
        help_text="Deterministic product selection rule for the storefront homepage.",
    )
    homepage_product_limit = models.PositiveSmallIntegerField(
        default=8,
        help_text="Maximum number of products in the homepage selection (4-24).",
    )
    domain = models.CharField(
        max_length=200, blank=True, default="",
        help_text="Custom domain override. Blank = <slug>.<STOREFRONT_BASE_DOMAIN> auto-derived.",
    )
    seo_title = models.CharField(max_length=70, blank=True, default="")
    seo_description = models.CharField(max_length=320, blank=True, default="")
    seo_social_image = models.URLField(max_length=500, blank=True, default="")
    allow_search_indexing = models.BooleanField(
        default=False,
        help_text="Merchant opt-in. Effective only for a healthy public production storefront.",
    )

    def clean(self):
        super().clean()
        import re
        from .design_access import can_use_design

        if self.brand_color and not re.match(HEX_COLOR_RE, self.brand_color):
            raise ValidationError({"brand_color": "Enter a 6-digit hex color, e.g. #1F8A8C."})
        if not 4 <= self.homepage_product_limit <= 24:
            raise ValidationError({"homepage_product_limit": "Choose between 4 and 24 products."})
        validate_social_links(self.social_links)
        validate_storefront_appearance(self.appearance)
        if not can_use_design(self.plan, self.design):
            raise ValidationError({
                "design": "This premium design requires an active Premium plan."
            })

    def __str__(self):
        return f"{self.store.slug} config"


class StorefrontDomain(models.Model):
    """A verified hostname binding. Submitted names are never trusted directly."""

    class Kind(models.TextChoices):
        PLATFORM = "platform", "Platform fallback"
        CUSTOM = "custom", "Merchant custom domain"
        PROVIDER = "provider", "Provider domain"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AWAITING_OWNERSHIP = "awaiting_ownership", "Awaiting ownership proof"
        AWAITING_DNS = "awaiting_dns", "Awaiting routing DNS"
        VERIFYING = "verifying", "Verifying"
        ACTIVE = "active", "Active"
        FAILED = "failed", "Needs attention"
        REMOVING = "removing", "Removing"

    class CertificateStatus(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        FAILED = "failed", "Failed"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="storefront_domains")
    hostname = models.CharField(max_length=253, unique=True, db_index=True)
    display_hostname = models.CharField(max_length=253, blank=True, default="")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CUSTOM)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    is_primary = models.BooleanField(default=False)
    ownership_token = models.CharField(max_length=96, blank=True, default="", editable=False)
    ownership_verified_at = models.DateTimeField(null=True, blank=True)
    expected_dns_records = models.JSONField(default=list, blank=True)
    provider_project_id = models.CharField(max_length=100, blank=True, default="")
    provider_verified = models.BooleanField(default=False)
    certificate_status = models.CharField(max_length=16, choices=CertificateStatus.choices, default=CertificateStatus.UNKNOWN)
    health_verified_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    public_message = models.CharField(max_length=240, blank=True, default="")
    internal_diagnostics = models.TextField(blank=True, default="", editable=False)
    operation_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    operation = models.CharField(max_length=16, blank=True, default="", editable=False)
    operation_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_primary", "kind", "hostname"]
        constraints = [
            models.UniqueConstraint(
                fields=["store"], condition=models.Q(is_primary=True),
                name="uniq_primary_storefront_domain_per_store",
            ),
            models.CheckConstraint(
                condition=models.Q(is_primary=False) | models.Q(status="active"),
                name="primary_storefront_domain_must_be_active",
            ),
        ]

    @property
    def healthy(self):
        return bool(
            self.status == self.Status.ACTIVE
            and self.provider_verified
            and self.certificate_status == self.CertificateStatus.VALID
            and self.health_verified_at
        )

    def __str__(self):
        return f"{self.hostname} ({self.status})"


class StorefrontDomainEvent(models.Model):
    domain = models.ForeignKey(StorefrontDomain, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=64)
    public_message = models.CharField(max_length=240, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class StorefrontPage(models.Model):
    """Editable page document with an explicit draft/publish boundary."""

    class PageKeys(models.TextChoices):
        HOME = "home", "Homepage"
        CATALOG = "catalog", "Products"
        PRODUCT = "product", "Product page"
        ACCOUNT = "account", "Customer account"
        LOGIN = "login", "Customer sign in"

    class PageTypes(models.TextChoices):
        SYSTEM = "system", "System page"
        CONTENT = "content", "Content page"
        POLICY = "policy", "Policy page"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="storefront_pages")
    page_key = models.SlugField(max_length=60, default=PageKeys.HOME)
    page_type = models.CharField(max_length=16, choices=PageTypes.choices, default=PageTypes.CONTENT)
    route = models.SlugField(max_length=80, blank=True, default="")
    title = models.JSONField(default=localized_text)
    is_visible = models.BooleanField(default=True)
    show_in_navigation = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    draft_sections = models.JSONField(default=default_homepage_sections, validators=[validate_storefront_sections])
    published_sections = models.JSONField(default=default_homepage_sections, validators=[validate_storefront_sections])
    revision = models.PositiveIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storefront_page_updates",
    )

    class Meta:
        ordering = ["store_id", "sort_order", "page_key"]
        constraints = [
            models.UniqueConstraint(fields=["store", "page_key"], name="uniq_storefront_page_key"),
            models.UniqueConstraint(fields=["store", "route"], name="uniq_storefront_page_route"),
        ]

    def clean(self):
        super().clean()
        validate_storefront_page_sections(self.page_key, self.draft_sections)
        validate_storefront_page_sections(self.page_key, self.published_sections)
        _validate_localized_text(self.title, "Page title", required=True, max_length=120)

    @property
    def is_protected(self):
        return self.page_key in SYSTEM_STOREFRONT_PAGE_KEYS

    def __str__(self):
        return f"{self.store.slug}/{self.page_key} v{self.revision}"


def ensure_default_storefront_pages(store):
    try:
        config = store.config
    except StoreConfig.DoesNotExist:
        config = None
    pages = []
    for spec in default_storefront_page_specs(config):
        lookup = {"store": store, "page_key": spec["page_key"]}
        defaults = {key: deepcopy(value) for key, value in spec.items() if key != "page_key"}
        page, created = StorefrontPage.objects.get_or_create(**lookup, defaults=defaults)
        if not created and page.page_key in SYSTEM_STOREFRONT_PAGE_KEYS and not any(page.title.values()):
            page.title = deepcopy(spec["title"])
            page.page_type = spec["page_type"]
            page.route = spec["route"]
            page.show_in_navigation = spec["show_in_navigation"]
            page.sort_order = spec["sort_order"]
            page.save(update_fields=["title", "page_type", "route", "show_in_navigation", "sort_order", "updated_at"])
        pages.append(page)
    return pages


class StorefrontAsset(models.Model):
    """A merchant-uploaded visual referenced by a storefront page document."""

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="storefront_assets")
    image = models.ImageField(upload_to=storefront_asset_path)
    created_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="storefront_asset_uploads",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.store.slug}: {self.image.name}"


def sync_homepage_merchandising(config, *, publish=True):
    """Keep the legacy compact setting and the page document compatible."""
    sections = homepage_sections_for_config(config)
    page, created = StorefrontPage.objects.get_or_create(
        store=config.store,
        page_key=StorefrontPage.PageKeys.HOME,
        defaults={"draft_sections": sections, "published_sections": deepcopy(sections)},
    )
    if created:
        return page
    changed = False
    documents = ["draft_sections"] + (["published_sections"] if publish else [])
    for field in documents:
        document = deepcopy(getattr(page, field))
        product_section = next((section for section in document if section.get("type") == "products"), None)
        if product_section is None:
            continue
        settings_value = product_section.get("settings", {})
        if (
            settings_value.get("source") != config.homepage_product_source
            or settings_value.get("limit") != config.homepage_product_limit
        ):
            settings_value["source"] = config.homepage_product_source
            settings_value["limit"] = config.homepage_product_limit
            setattr(page, field, document)
            changed = True
    if changed:
        update_fields = documents + ["updated_at"]
        if publish:
            from django.utils import timezone

            page.revision += 1
            page.published_at = timezone.now()
            update_fields += ["revision", "published_at"]
        page.save(update_fields=update_fields)
    return page


class StoreMember(models.Model):
    """A user's role *within a store* (role-based commerce staff equivalent).

    This is where authorization lives: 'can this user deploy/manage this
    store?' is answered by a StoreMember row — never by a role string on
    the user model."""
    class Roles(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        STAFF = "staff", "Staff"
        DELIVERY = "delivery", "Delivery"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="store_memberships",
    )
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="members")
    role = models.CharField(max_length=20, choices=Roles.choices, default=Roles.STAFF)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "store")
        constraints = [
            models.UniqueConstraint(
                fields=["store"],
                condition=models.Q(role="owner"),
                name="uniq_owner_per_store",
            ),
        ]

    def __str__(self):
        return f"{self.user.email} @ {self.store.slug} ({self.role})"


class ClientProfile(models.Model):
    """A storefront customer account. Scoped to the stores it belongs to —
    the same person can be a customer of several stores, and a merchant of
    another entirely (they're separate concepts from StoreMember)."""
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="client_profile",
    )
    stores = models.ManyToManyField(Store, blank=True, related_name="customers")
    class PreferredLanguage(models.TextChoices):
        ENGLISH = "en", "English"
        FRENCH = "fr", "French"
        ARABIC = "ar", "Arabic"

    phone = PhoneNumberField(blank=True, default="", region=None)
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    address = models.TextField(blank=True, default="")
    city = models.CharField(max_length=50, blank=True, default="")
    country = models.CharField(max_length=50, blank=True, default="")
    preferred_language = models.CharField(
        max_length=2,
        choices=PreferredLanguage.choices,
        default=PreferredLanguage.ARABIC,
    )
    loyalty_points = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_phone_verified(self):
        return self.phone_verified_at is not None

    def __str__(self):
        return f"{self.user.email} (client)"


class StoreCustomer(models.Model):
    """A customer's tenant-specific relationship with one merchant store."""

    profile = models.ForeignKey(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name="store_customers",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="store_customers",
    )
    loyalty_points = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "store"],
                name="uniq_customer_per_store",
            ),
        ]

    def __str__(self):
        return f"{self.profile.user.email} @ {self.store.slug}"
