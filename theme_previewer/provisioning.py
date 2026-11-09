import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from importlib.resources import files

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from api.designs import get_design
from api.serializers import derive_palette
from catalog.models import AttributeDef, CatalogSection, Product, ProductImage, StockVariant
from store.models import Store, StoreConfig, default_storefront_appearance, ensure_default_storefront_pages
from storefront_factory.models import StorefrontRequest
from .models import ThemeDemoStore


def catalog_definitions() -> dict:
    return json.loads(files("theme_previewer").joinpath("catalogs.json").read_text())


def _catalog_revision(catalog: dict) -> str:
    return hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _product_art(name: str, ref: str, brand: str, index: int) -> bytes:
    safe_name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    accent = "#F4E8D2" if index % 2 == 0 else "#DDE8E5"
    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='1500' viewBox='0 0 1200 1500'>
    <rect width='1200' height='1500' fill='{accent}'/><circle cx='920' cy='280' r='260' fill='{brand}' opacity='.16'/>
    <rect x='270' y='300' width='660' height='760' rx='90' fill='{brand}' opacity='.86'/>
    <circle cx='600' cy='610' r='180' fill='white' opacity='.22'/><path d='M380 1040h440' stroke='white' stroke-width='18' opacity='.55'/>
    <text x='600' y='1250' text-anchor='middle' font-family='Arial,sans-serif' font-size='56' font-weight='700' fill='#15202b'>{safe_name}</text>
    <text x='600' y='1325' text-anchor='middle' font-family='Arial,sans-serif' font-size='26' letter-spacing='8' fill='#52606d'>{ref}</text></svg>"""
    return svg.encode()


def _product_image(item: dict, theme_key: str, brand: str, index: int) -> tuple[str, bytes]:
    asset = item.get("image_asset")
    if asset:
        asset_path = files("theme_previewer").joinpath("assets", asset)
        return f"theme-demos/{theme_key}/{item['ref']}.webp", asset_path.read_bytes()
    return f"theme-demos/{theme_key}/{item['ref']}.svg", _product_art(item["name"], item["ref"], brand, index)


@transaction.atomic
def provision_theme_demo(theme_key: str, *, public_url: str = "", enable: bool = False) -> ThemeDemoStore:
    design = get_design(theme_key)
    if design.get("status") != "ready":
        raise ValueError(f"Theme {theme_key!r} is not production-ready.")
    catalogs = catalog_definitions()
    catalog = catalogs.get(theme_key)
    if not catalog:
        raise ValueError(f"Add theme_previewer/catalogs.json content for {theme_key!r} before provisioning.")

    store, _ = Store.objects.update_or_create(
        slug=f"demo-{theme_key}", defaults={"name": catalog["store_name"]},
    )
    if store.members.exists():
        raise ValueError("Official demo tenants cannot have merchant memberships.")
    config, _ = StoreConfig.objects.get_or_create(store=store)
    appearance = deepcopy(default_storefront_appearance())
    appearance["header"]["show_account"] = False
    appearance["footer"]["show_contact"] = False
    appearance["footer"]["show_social_links"] = False
    config.design = theme_key
    config.plan = StoreConfig.Plans.PREMIUM if design.get("tier") == "premium" else StoreConfig.Plans.FREE
    config.currency = "MAD"
    config.default_locale = "en"
    config.brand_color = catalog["brand_color"]
    config.contact_email = ""
    config.payment_keys = {}
    config.appearance = appearance
    config.homepage_product_source = StoreConfig.HomepageProductSources.FEATURED
    config.homepage_product_limit = 8
    config.full_clean()
    config.save()

    pages = ensure_default_storefront_pages(store)
    home = next(page for page in pages if page.page_key == "home")
    sections = deepcopy(home.published_sections)
    hero = next((item for item in sections if item.get("type") == "hero"), None)
    if hero:
        hero["settings"]["eyebrow"] = {"en": design["tagline"], "fr": design["tagline"], "ar": design["tagline"]}
        hero["settings"]["title"] = {"en": catalog["store_name"], "fr": catalog["store_name"], "ar": catalog["store_name"]}
        hero["settings"]["subtitle"] = {"en": design["description"], "fr": design["description"], "ar": design["description"]}
    home.draft_sections = sections
    home.published_sections = deepcopy(sections)
    home.save(update_fields=["draft_sections", "published_sections", "updated_at"])

    wanted_sections = set()
    section_rows = {}
    for order, item in enumerate(catalog["sections"], 1):
        section, _ = CatalogSection.objects.update_or_create(
            store=store, key=item["key"],
            defaults={"label": item["label"], "icon": item["icon"], "sort_order": order, "is_visible": True},
        )
        wanted_sections.add(section.pk)
        section_rows[item["key"]] = section
        AttributeDef.objects.update_or_create(
            store=store, section=section, key="option",
            defaults={"label": "Option", "type": "select", "level": "variant", "required": True, "sort_order": 1,
                      "options": [{"value": value, "label": value} for product in catalog["products"] if product["section"] == item["key"] for value in product["colors"]]},
        )

    wanted_products = set()
    for index, item in enumerate(catalog["products"]):
        options = list(dict.fromkeys(item["colors"]))
        product, created = Product.objects.update_or_create(
            store=store, ref=item["ref"],
            defaults={"section": section_rows[item["section"]], "name": item["name"], "slug": slugify(item["name"]),
                      "description": item["description"], "price": Decimal(item["price"]), "promo": 0,
                      "rating": 0, "reviews_count": 0, "uses_variants": True, "is_active": True,
                      "is_featured": True, "merchandising_order": index,
                      "variant_options": [{"key": "option", "label": "Option", "type": "select",
                                           "values": [{"value": option, "label": option} for option in options]}]},
        )
        wanted_products.add(product.pk)
        wanted_skus = set()
        for option_index, option in enumerate(options):
            sku = f"DEMO-{theme_key.upper()}-{item['ref']}-{option_index + 1}"
            wanted_skus.add(sku)
            StockVariant.objects.update_or_create(
                store=store, sku=sku,
                defaults={"product": product, "attributes": {"option": option}, "price": Decimal(item["price"]), "quantity": 40 + index * 7, "reserved": 0},
            )
        product.variants.exclude(sku__in=wanted_skus).delete()
        if not product.images.exists() or item.get("image_asset"):
            if item.get("image_asset"):
                product.images.all().delete()
            image = ProductImage(product=product, is_primary=True, order=0, alt_text=item["name"])
            filename, content = _product_image(item, theme_key, catalog["brand_color"], index)
            image.image.save(filename, ContentFile(content), save=True)

    store.products.exclude(pk__in=wanted_products).delete()
    store.catalog_sections.exclude(pk__in=wanted_sections).delete()

    deployment_palette = derive_palette(config.brand_color)
    deployment, created_deployment = StorefrontRequest.objects.get_or_create(
        slug=store.slug,
        defaults={
            "store": store, "name": store.name, "design": theme_key,
            "domain": "https://CHANGE-ME.example.com", "contact_email": "",
            "currency": config.currency, **deployment_palette,
        },
    )
    if deployment.store_id != store.id or deployment.design != theme_key:
        raise ValueError("The demo storefront slug is already assigned to another store or theme.")

    profile_defaults = {
        "store": store, "deployment": deployment,
        "catalog_revision": _catalog_revision(catalog),
    }
    if public_url:
        profile_defaults["public_url"] = public_url
    profile, _ = ThemeDemoStore.objects.update_or_create(
        theme_key=theme_key,
        defaults=profile_defaults,
    )
    if created_deployment and not enable:
        profile.deployment_status = ThemeDemoStore.DeploymentStatus.PREPARED
        profile.save(update_fields=["deployment_status", "updated_at"])
    if enable:
        if not public_url:
            raise ValueError("A validated HTTPS public URL is required to enable a demo.")
        profile.deployment_status = ThemeDemoStore.DeploymentStatus.READY
        profile.is_enabled = True
        profile.last_validated_at = timezone.now()
        profile.full_clean()
        profile.save()
    return profile
