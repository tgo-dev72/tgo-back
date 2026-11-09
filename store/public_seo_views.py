"""Tenant-isolated robots and sitemap documents for generated storefronts."""

from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.views import View
from django.http import JsonResponse
from django.utils.html import strip_tags

from catalog.models import CatalogSection, CatalogSectionSlugRedirect, Product, ProductSlugRedirect

from .models import Store, StorefrontPage
from .seo import storefront_seo_policy


NO_STORE = "private, no-store"


def _response(body, *, content_type, status=200, indexable=False):
    response = HttpResponse(body, content_type=content_type, status=status)
    response["Cache-Control"] = NO_STORE
    if not indexable:
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _store(slug):
    return get_object_or_404(Store.objects.select_related("config"), slug=slug)


class StorefrontRobotsView(View):
    def get(self, request, slug):
        store = _store(slug)
        policy = storefront_seo_policy(store)
        if not policy.indexable:
            return _response("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /account\n"
            "Disallow: /checkout\n"
            "Disallow: /*?search=\n"
            f"Sitemap: {policy.canonical_origin}/sitemap.xml\n"
        )
        return _response(body, content_type="text/plain; charset=utf-8", indexable=True)


class StorefrontSitemapView(View):
    def get(self, request, slug):
        store = _store(slug)
        policy = storefront_seo_policy(store)
        if not policy.indexable:
            raise Http404("Storefront sitemap is unavailable.")

        entries = {("/", None)}
        entries.update(
            (f"/collections/{quote(key, safe='')}", None)
            for key in CatalogSection.objects.filter(store=store, is_visible=True)
            .order_by("key").values_list("key", flat=True)
        )
        entries.update(
            (f"/products/{quote(product.slug or str(product.pk), safe='')}", product.updated_at)
            for product in Product.objects.filter(store=store, is_active=True)
            .only("pk", "slug", "updated_at").order_by("slug", "pk")
        )
        private_keys = {
            StorefrontPage.PageKeys.HOME, StorefrontPage.PageKeys.CATALOG,
            StorefrontPage.PageKeys.PRODUCT, StorefrontPage.PageKeys.ACCOUNT,
            StorefrontPage.PageKeys.LOGIN,
        }
        entries.update(
            (f"/{quote(page.route, safe='')}", page.updated_at)
            for page in StorefrontPage.objects.filter(
                store=store, is_visible=True, published_at__isnull=False,
            ).exclude(page_key__in=private_keys).exclude(route="")
            .only("route", "updated_at").order_by("route")
        )

        urlset = Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for path, modified in sorted(entries, key=lambda item: item[0]):
            node = SubElement(urlset, "url")
            SubElement(node, "loc").text = f"{policy.canonical_origin}{path}"
            if modified:
                SubElement(node, "lastmod").text = modified.date().isoformat()
        xml = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(urlset, encoding="utf-8")
        return _response(xml, content_type="application/xml; charset=utf-8", indexable=True)


def _asset_url(field):
    if not field:
        return ""
    try:
        value = field.url
    except (AttributeError, ValueError):
        return ""
    return value if str(value).startswith("https://") else ""


def _plain(value):
    return " ".join(strip_tags(str(value or "")).split())


def _localized(value, locale, fallback=""):
    if not isinstance(value, dict):
        return _plain(value) or fallback
    return _plain(value.get(locale) or value.get("fr") or value.get("en") or value.get("ar")) or fallback


class StorefrontMetadataView(View):
    """Metadata contract consumed by the generated first-response renderer."""

    def get(self, request, slug):
        store = _store(slug)
        config = store.config
        policy = storefront_seo_policy(store, config)
        supplied_path = str(request.GET.get("path", "/"))
        has_query = "?" in supplied_path or bool(request.GET.get("search"))
        raw_path = supplied_path.split("?", 1)[0].split("#", 1)[0]
        path = "/" + raw_path.strip("/") if raw_path.strip("/") else "/"
        canonical_path = "/" + "/".join(quote(segment, safe="") for segment in path.strip("/").split("/")) if path != "/" else "/"
        locale = request.GET.get("locale") if request.GET.get("locale") in {"en", "fr", "ar"} else config.default_locale
        store_description = _plain(config.seo_description) or {
            "en": f"Discover products and shop from {store.name}.",
            "fr": f"Découvrez les produits de {store.name}.",
            "ar": f"اكتشف منتجات {store.name}.",
        }[locale]
        store_title = _plain(config.seo_title) or store.name
        social_image = config.seo_social_image or _asset_url(store.logo)
        canonical = policy.canonical_origin + canonical_path
        indexable = policy.indexable
        status = 200
        page_type = "website"
        title = store_title
        description = store_description
        schemas = []

        if path == "/":
            schemas.extend([
                {"@context": "https://schema.org", "@type": "OnlineStore", "name": store.name, "url": canonical, "image": social_image or None},
                {"@context": "https://schema.org", "@type": "WebSite", "name": store.name, "url": canonical, "inLanguage": locale},
            ])
        elif path.startswith("/products/"):
            identifier = path.removeprefix("/products/")
            product = Product.objects.filter(store=store, is_active=True, slug=identifier).prefetch_related("images", "variants", "vendors").first()
            if product is None:
                redirect = ProductSlugRedirect.objects.filter(
                    store=store, old_slug=identifier, product__is_active=True,
                ).select_related("product").first()
                if redirect:
                    canonical = f"{policy.canonical_origin}/products/{quote(redirect.product.slug, safe='')}"
                    status, indexable = 308, False
                    title, description = f"{redirect.product.name} | {store.name}", store_description
                else:
                    status, indexable = 404, False
                    title, description = f"Product unavailable | {store.name}", store_description
            else:
                title = _plain(product.seo_title) or f"{product.name} | {store.name}"
                description = _plain(product.seo_description) or _plain(product.description) or store_description
                product_image = product.seo_social_image or next((_asset_url(item.image) for item in product.images.all() if _asset_url(item.image)), "")
                social_image = product_image or social_image
                in_stock = not product.track_inventory or any(item.available_quantity() > 0 for item in product.variants.all())
                brand = product.vendors.first()
                schemas.append({
                    "@context": "https://schema.org", "@type": "Product", "name": product.name,
                    "description": description, "image": [_asset_url(item.image) for item in product.images.all() if _asset_url(item.image)],
                    "sku": product.ref or None, "brand": ({"@type": "Brand", "name": brand.name} if brand else None),
                    "offers": {"@type": "Offer", "url": canonical, "price": str(product.price),
                               "priceCurrency": config.currency, "availability": "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock",
                               "seller": {"@type": "Organization", "name": store.name}},
                })
                page_type = "product"
        elif path.startswith("/collections/"):
            key = path.removeprefix("/collections/")
            if key == "all":
                title, description = f"Products | {store.name}", store_description
            else:
                section = CatalogSection.objects.filter(store=store, is_visible=True, key=key).first()
                if section is None:
                    redirect = CatalogSectionSlugRedirect.objects.filter(
                        store=store, old_slug=key, section__is_visible=True,
                    ).select_related("section").first()
                    if redirect:
                        canonical = f"{policy.canonical_origin}/collections/{quote(redirect.section.key, safe='')}"
                        status, indexable = 308, False
                        title, description = f"{redirect.section.label} | {store.name}", store_description
                    else:
                        status, indexable = 404, False
                        title, description = f"Collection unavailable | {store.name}", store_description
                else:
                    title = _plain(section.seo_title) or f"{section.label} | {store.name}"
                    description = _plain(section.seo_description) or _plain(section.description) or store_description
                    social_image = section.seo_social_image or _asset_url(section.thumbnail) or social_image
                    schemas.append({"@context": "https://schema.org", "@type": "CollectionPage", "name": section.label, "description": description, "url": canonical})
        elif path in {"/account", "/checkout", "/cart"}:
            indexable = False
            title, description = f"Customer account | {store.name}", store_description
        else:
            route = path.strip("/")
            page = StorefrontPage.objects.filter(
                store=store, route=route, is_visible=True, published_at__isnull=False,
            ).exclude(page_key__in={StorefrontPage.PageKeys.ACCOUNT, StorefrontPage.PageKeys.LOGIN}).first()
            if page is None:
                status, indexable = 404, False
                title, description = f"Page not found | {store.name}", store_description
            else:
                title = f"{_localized(page.title, locale, route.replace('-', ' ').title())} | {store.name}"

        if has_query:
            indexable = False
        if path != "/" and status == 200:
            schemas.append({
                "@context": "https://schema.org", "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": store.name, "item": policy.canonical_origin + "/"},
                    {"@type": "ListItem", "position": 2, "name": title.split(" | ")[0], "item": canonical},
                ],
            })
        payload = {
            "status": status, "locale": locale, "title": title, "description": description,
            "canonical": canonical, "robots": "index, follow, max-image-preview:large" if indexable else "noindex, nofollow, noarchive",
            "openGraph": {"type": page_type, "title": title, "description": description, "url": canonical, "image": social_image or None},
            "twitter": {"card": "summary_large_image" if social_image else "summary", "title": title, "description": description, "image": social_image or None},
            "structuredData": schemas,
            "redirect": canonical if status == 308 else None,
        }
        response = JsonResponse(payload, status=status)
        response["Cache-Control"] = NO_STORE
        if not indexable:
            response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response
