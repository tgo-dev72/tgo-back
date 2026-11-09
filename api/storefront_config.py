"""Public storefront config endpoint — the Option-A contract.

The generic storefront app (one shared Vercel deployment for ALL stores) calls
GET /api/storefront/config/ at boot. The store is resolved from the request
like every other public endpoint: the subdomain in production
(mystore.tijarago.com -> mystore), or the X-Store-Slug header in local
dev. The response is the full runtime store config — name, palette, design,
sections, shipping/currency — so the SAME static bundle renders any store with
zero per-store env vars or build steps.
"""
from copy import deepcopy
from html.parser import HTMLParser

from django.core.exceptions import ObjectDoesNotExist
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from common.media_uploads import media_asset_url, normalize_direct_upload_url
from store.context import resolve_store
from store.design_access import effective_design
from store.models import StoreConfig, ensure_default_storefront_pages, normalize_storefront_appearance
from theme_previewer.registry import signup_url as theme_preview_signup_url
from theme_previewer.presets import apply_demo_preset
from theme_previewer.tickets import validate_preview_ticket
from subscriptions.entitlements import owner_has_store_entitlement
from subscriptions.policies import REMOVE_BRANDING_KEY
from subscriptions.access import store_has_active_subscription
from store.seo import storefront_seo_policy

from .serializers import derive_palette

# Store config is static per store and changes rarely — cache it at the CDN
# edge (Vercel/Cloudflare) so storefront boot is a near-instant cache hit.
# Keep the public boot payload fast while ensuring an explicit merchant
# publication becomes visible within seconds rather than several minutes.
CACHE_CONTROL = "s-maxage=15, stale-while-revalidate=30"

# backend color_* fields -> the storefront's StorePalette shape
PALETTE_KEYS = {
    "color_brand": "brand",
    "color_brand_dark": "brandDark",
    "color_brand_deep": "brandDeep",
    "color_ink": "ink",
    "color_brand_light": "brandLight",
    "color_brand_light_1": "brandLight1",
    "color_brand_light_2": "brandLight2",
}

DEFAULT_SECTIONS = [
    {"key": "men", "label": "Men", "icon": "FaTshirt:fa"},
    {"key": "women", "label": "Women", "icon": "FaBagShopping:fa6"},
    {"key": "accessories", "label": "Accessories", "icon": "FaBoxOpen:fa"},
]


class _PlainSectionText(HTMLParser):
    """Readable copy for storefront bundles that predate formatted sections."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "h2", "h3", "blockquote", "li", "br"} and self.parts:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def _legacy_section_text(value):
    if not isinstance(value, str) or "<" not in value:
        return value
    parser = _PlainSectionText()
    parser.feed(value)
    parser.close()
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def _sections_for_client(sections, *, rich_text):
    client_sections = deepcopy(sections)
    for section in client_sections:
        settings = section.get("settings", {})
        for key in ("image_url", "mobile_image_url"):
            if isinstance(settings.get(key), str):
                settings[key] = normalize_direct_upload_url(settings[key])
        for slide in settings.get("slides", []):
            for key in ("image_url", "mobile_image_url"):
                if isinstance(slide.get(key), str):
                    slide[key] = normalize_direct_upload_url(slide[key])
        if not rich_text:
            for key in ("body", "subtitle"):
                if isinstance(settings.get(key), dict):
                    settings[key] = {locale: _legacy_section_text(value) for locale, value in settings[key].items()}
            for slide in settings.get("slides", []):
                if isinstance(slide.get("body"), dict):
                    slide["body"] = {locale: _legacy_section_text(value) for locale, value in slide["body"].items()}
    return client_sections


def _palette(brand_color: str) -> dict[str, str]:
    return {front_key: derive_palette(brand_color)[back_key] for back_key, front_key in PALETTE_KEYS.items()}


def _storefront_domain(store, config) -> str:
    """Use the same public address as deployment and customer emails."""
    from storefront_factory.domains import public_storefront_url

    return public_storefront_url(store, config)


class StorefrontConfigView(APIView):
    """GET /api/storefront/config/ — runtime config for the resolved store."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        store = resolve_store(request)
        rich_text = request.query_params.get("rich_text") == "1"
        config, _ = StoreConfig.objects.get_or_create(store=store)
        try:
            demo_profile = store.theme_demo_profile
        except ObjectDoesNotExist:
            demo_profile = None
        if demo_profile and not validate_preview_ticket(
            request.headers.get("X-Preview-Ticket", ""),
            theme_key=demo_profile.theme_key,
            store_slug=store.slug,
        ):
            return Response(
                {"code": "preview_access_denied", "detail": "This preview session is unavailable."},
                status=403,
                headers={"Cache-Control": "no-store"},
            )
        if not demo_profile and not store_has_active_subscription(store):
            return Response(
                {
                    "code": "storefront_temporarily_unavailable",
                    "detail": "This store is temporarily unavailable.",
                    "storeName": store.name,
                },
                status=402,
                headers={"Cache-Control": "no-store", "Vary": "X-Store-Slug"},
            )
        pages = ensure_default_storefront_pages(store)
        # Derive access from live subscriptions on every cache miss. This both
        # removes expired access and grants access after an ownership change,
        # even when the denormalized StoreConfig cache was stale in either
        # direction.
        if not demo_profile:
            from subscriptions.services import sync_store_entitlement
            sync_store_entitlement(store)
            config.refresh_from_db(fields=["plan"])

        sections = list(
            store.catalog_sections.order_by("sort_order", "key").values("key", "label", "icon")
        )
        if not sections:
            sections = DEFAULT_SECTIONS
        try:
            from payments.gateways import get_gateway
            payment_gateway = get_gateway(config.payment_provider)
            payment_available = payment_gateway.public_descriptor(
                payment_gateway.store_credentials(config),
            )["configured"]
        except Exception:
            payment_available = False

        data = {
            "storeName": store.name,
            "slug": store.slug,
            "domain": _storefront_domain(store, config),
            "canonicalOrigin": storefront_seo_policy(store, config).canonical_origin,
            "indexingAllowed": storefront_seo_policy(store, config).indexable and not bool(demo_profile),
            "seo": {
                "title": config.seo_title,
                "description": config.seo_description,
                "socialImage": config.seo_social_image,
            },
            "publicDomains": [
                {"hostname": item.hostname, "primary": item.is_primary}
                for item in store.storefront_domains.filter(
                    status="active", provider_verified=True,
                    certificate_status="valid", health_verified_at__isnull=False,
                )
            ],
            "contactEmail": config.contact_email,
            "phone": str(config.support_phone) if config.support_phone else "",
            "whatsappPhone": str(config.whatsapp_phone) if config.whatsapp_phone else "",
            "socialLinks": config.social_links,
            "appearance": normalize_storefront_appearance(config.appearance),
            "logoUrl": media_asset_url(store.logo, request=request) or None,
            "currency": config.currency,
            "palette": _palette(config.brand_color),
            "design": effective_design(config.plan, config.design),
            # Keep the saved preference on downgrade, but derive customer-facing
            # attribution from the live entitlement on every cache miss.
            "showPlatformAttribution": not (
                config.remove_platform_branding
                and not demo_profile
                and owner_has_store_entitlement(store, REMOVE_BRANDING_KEY)
            ),
            "freeShippingThreshold": (
                config.free_shipping_threshold
                if config.free_shipping_enabled
                and config.free_shipping_currency == config.currency
                else None
            ),
            "shippingFee": config.shipping_fee,
            "defaultLocale": config.default_locale,
            "homepage": {
                "productSource": config.homepage_product_source,
                "productLimit": config.homepage_product_limit,
            },
            "pages": {
                page.page_key: {
                    "type": page.page_type,
                    "route": page.route,
                    "title": page.title,
                    "showInNavigation": page.show_in_navigation,
                    "sections": _sections_for_client(page.published_sections, rich_text=rich_text),
                    "revision": page.revision,
                }
                for page in pages
                if page.is_visible
            },
            "payment": {
                "cod": config.cod_enabled,
                "online": payment_available and not bool(demo_profile),
                "acceptedCards": [],
            },
            "demo": ({
                "enabled": True,
                "signupUrl": theme_preview_signup_url(demo_profile.theme_key),
            } if demo_profile else None),
            "sections": sections,
        }
        if demo_profile:
            data = apply_demo_preset(data, demo_profile.theme_key, request.headers.get("X-Preview-Preset", ""))
        resp = Response(data)
        resp["Cache-Control"] = CACHE_CONTROL
        # Store identity travels in X-Store-Slug (the Host is always the
        # platform's), so the CDN must key the cache on it — otherwise every
        # store gets the first store's cached config.
        resp["Vary"] = "X-Store-Slug"
        return resp
