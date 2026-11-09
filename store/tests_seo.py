from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from catalog.models import CatalogSection, CatalogSectionSlugRedirect, Product, ProductSlugRedirect
from storefront_factory.models import StorefrontRequest
from theme_previewer.models import ThemeDemoStore

from .models import Store, StoreMember, StorefrontDomain, StorefrontPage
from .seo import canonical_storefront_origin, storefront_seo_policy


@override_settings(
    ALLOWED_HOSTS=["testserver", "attacker.example"],
    DEBUG=False,
    STOREFRONT_INDEXING_ENABLED=True,
    STOREFRONT_BASE_DOMAIN="tijarago.com",
)
class StorefrontSeoPolicyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="seo-owner@example.test", password="password123",
        )
        MerchantProfile.objects.create(user=self.user)
        self.store = Store.objects.create(slug="atelier-seo", name="Atelier & Fils")
        StoreMember.objects.create(store=self.store, user=self.user, role=StoreMember.Roles.OWNER)
        self.store.config.allow_search_indexing = True
        self.store.config.seo_title = "Créations marocaines"
        self.store.config.seo_description = "منتجات مغربية أصلية & artisanat français"
        self.store.config.save()
        self.publication = StorefrontRequest.objects.create(
            store=self.store, slug=self.store.slug, name=self.store.name, design="basic",
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            pipeline_state=StorefrontRequest.PIPELINE_LIVE,
            progress=100,
            completed_at=timezone.now(),
        )
        self.platform_domain = self.store.storefront_domains.get(kind=StorefrontDomain.Kind.PLATFORM)
        self.platform_domain.hostname = "atelier-seo.tijarago.com"
        self.platform_domain.status = StorefrontDomain.Status.ACTIVE
        self.platform_domain.is_primary = True
        self.platform_domain.provider_verified = True
        self.platform_domain.certificate_status = StorefrontDomain.CertificateStatus.VALID
        self.platform_domain.health_verified_at = timezone.now()
        self.platform_domain.save(
            update_fields=["hostname", "status", "is_primary", "provider_verified", "certificate_status", "health_verified_at"],
        )

    def test_default_origin_is_server_authoritative_and_header_independent(self):
        self.assertEqual(canonical_storefront_origin(self.store), "https://atelier-seo.tijarago.com")
        client = APIClient()
        response = client.get(
            "/api/storefront/config/",
            HTTP_X_STORE_SLUG=self.store.slug,
            HTTP_HOST="attacker.example",
            HTTP_X_FORWARDED_HOST="other-tenant.tijarago.com",
            HTTP_ORIGIN="https://preview-attacker.vercel.app",
            HTTP_REFERER="https://attacker.example/path?token=secret",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["canonicalOrigin"], "https://atelier-seo.tijarago.com")
        self.assertTrue(response.data["indexingAllowed"])

    def test_only_healthy_primary_custom_domain_becomes_canonical(self):
        self.platform_domain.is_primary = False
        self.platform_domain.save(update_fields=["is_primary"])
        pending = StorefrontDomain.objects.create(
            store=self.store, hostname="pending.example.ma", kind=StorefrontDomain.Kind.CUSTOM,
            status=StorefrontDomain.Status.AWAITING_DNS,
        )
        self.assertEqual(canonical_storefront_origin(self.store), "https://atelier-seo.tijarago.com")
        pending.status = StorefrontDomain.Status.ACTIVE
        pending.is_primary = True
        pending.provider_verified = True
        pending.certificate_status = StorefrontDomain.CertificateStatus.VALID
        pending.health_verified_at = timezone.now()
        pending.save()
        self.assertEqual(canonical_storefront_origin(self.store), "https://pending.example.ma")
        self.assertTrue(storefront_seo_policy(self.store).indexable)

    def test_every_safety_condition_fails_closed(self):
        self.store.config.allow_search_indexing = False
        self.assertEqual(storefront_seo_policy(self.store).reason, "merchant_opt_out")
        self.store.config.allow_search_indexing = True

        self.store.is_suspended = True
        self.assertEqual(storefront_seo_policy(self.store).reason, "store_suspended")
        self.store.is_suspended = False

        self.publication.delete()
        self.assertEqual(storefront_seo_policy(self.store).reason, "store_unpublished")
        self.publication = StorefrontRequest.objects.create(
            store=self.store, slug=self.store.slug, name=self.store.name, design="basic",
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            pipeline_state=StorefrontRequest.PIPELINE_LIVE,
        )

        self.platform_domain.health_verified_at = None
        self.platform_domain.save(update_fields=["health_verified_at"])
        self.assertEqual(storefront_seo_policy(self.store).reason, "domain_not_ready")
        self.platform_domain.health_verified_at = timezone.now()
        self.platform_domain.save(update_fields=["health_verified_at"])

        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(storefront_seo_policy(self.store).reason, "owner_inactive")

    @override_settings(STOREFRONT_INDEXING_ENABLED=False)
    def test_staging_or_disabled_environment_is_never_indexable(self):
        self.assertEqual(storefront_seo_policy(self.store).reason, "environment_disabled")

    def test_theme_demo_store_is_never_indexable(self):
        ThemeDemoStore.objects.create(theme_key="basic", store=self.store)
        self.assertEqual(storefront_seo_policy(self.store).reason, "preview_store")

    def test_non_indexable_robots_and_sitemap_fail_closed(self):
        self.store.config.allow_search_indexing = False
        self.store.config.save(update_fields=["allow_search_indexing"])
        robots = self.client.get(reverse("storefront-robots", args=[self.store.slug]))
        sitemap = self.client.get(reverse("storefront-sitemap", args=[self.store.slug]))
        self.assertEqual(robots.status_code, 200)
        self.assertEqual(robots.content, b"User-agent: *\nDisallow: /\n")
        self.assertEqual(robots["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertEqual(sitemap.status_code, 404)

    def test_tenant_sitemap_contains_only_published_canonical_resources(self):
        section = CatalogSection.objects.create(
            store=self.store, key="caftans", label="قفاطين & Caftans", is_visible=True,
        )
        hidden = CatalogSection.objects.create(
            store=self.store, key="hidden", label="Hidden", is_visible=False,
        )
        active = Product.objects.create(
            store=self.store, section=section, slug="caftan-été", name="قفطان Été",
            ref="SEO-1", price="99.00", is_active=True,
        )
        Product.objects.create(
            store=self.store, section=hidden, slug="private-product", name="Private",
            ref="SEO-2", price="10.00", is_active=False,
        )
        StorefrontPage.objects.update_or_create(
            store=self.store, page_key="about",
            defaults={
                "page_type": StorefrontPage.PageTypes.CONTENT, "route": "a-propos",
                "title": {"en": "About", "fr": "À propos", "ar": "من نحن"},
                "is_visible": True, "published_at": timezone.now(),
            },
        )
        other = Store.objects.create(slug="other-tenant", name="Other")
        other_section = CatalogSection.objects.create(store=other, key="other", label="Other")
        Product.objects.create(
            store=other, section=other_section, slug="foreign", name="Foreign",
            ref="OTHER", price="1.00", is_active=True,
        )

        response = self.client.get(reverse("storefront-sitemap", args=[self.store.slug]))
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("application/xml"))
        self.assertIn("https://atelier-seo.tijarago.com/collections/caftans", body)
        self.assertIn("https://atelier-seo.tijarago.com/products/caftan-%C3%A9t%C3%A9", body)
        self.assertIn("https://atelier-seo.tijarago.com/a-propos", body)
        self.assertNotIn("private-product", body)
        self.assertNotIn("foreign", body)
        self.assertNotIn("other-tenant", body)
        self.assertEqual(body.count(f"/products/{active.slug}"), 0)  # raw Unicode must be URL encoded

        robots = self.client.get(reverse("storefront-robots", args=[self.store.slug]))
        self.assertNotIn("X-Robots-Tag", robots)
        self.assertIn("Sitemap: https://atelier-seo.tijarago.com/sitemap.xml", robots.content.decode())

    def test_metadata_validation_accepts_unicode_and_rejects_html(self):
        client = APIClient(); client.force_authenticate(self.user)
        route = reverse("store-config", args=[self.store.pk])
        accepted = client.patch(route, {
            "seo_title": "قفاطين مغربية — Créations d’été",
            "seo_description": "وصف عربي avec accents français & symboles",
        }, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        rejected = client.patch(route, {"seo_title": "<script>alert(1)</script>"}, format="json")
        self.assertEqual(rejected.status_code, 400)

    def test_product_and_collection_metadata_use_tenant_data_and_safe_fallbacks(self):
        section = CatalogSection.objects.create(
            store=self.store, key="artisanat", label="Artisanat français & مغربي",
            description="Collection été & hiver", is_visible=True,
        )
        product = Product.objects.create(
            store=self.store, section=section, slug="lampe-artisanale", name='Lampe "Atlas" </script>',
            description="Création & ضوء", ref="LAMP-1", price="149.00", is_active=True,
        )
        product.variants.create(store=self.store, attributes={}, quantity=2, price="149.00")
        product_url = reverse("storefront-seo-metadata", args=[self.store.slug])
        response = self.client.get(product_url, {"path": "/products/lampe-artisanale"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["canonical"], "https://atelier-seo.tijarago.com/products/lampe-artisanale")
        self.assertIn('Lampe "Atlas" </script>', response.json()["title"])
        schema = response.json()["structuredData"][0]
        self.assertEqual(schema["offers"]["availability"], "https://schema.org/InStock")
        self.assertEqual(schema["offers"]["price"], "149.00")
        self.assertEqual(schema["offers"]["priceCurrency"], "MAD")
        self.assertNotIn("aggregateRating", schema)

        collection = self.client.get(product_url, {"path": "/collections/artisanat"})
        self.assertEqual(collection.status_code, 200)
        self.assertEqual(collection.json()["title"], "Artisanat français & مغربي | Atelier & Fils")
        self.assertEqual(collection.json()["structuredData"][0]["@type"], "CollectionPage")

        filtered = self.client.get(product_url, {"path": "/collections/artisanat?search=lampe"})
        self.assertEqual(filtered.json()["robots"], "noindex, nofollow, noarchive")

    def test_missing_or_cross_tenant_resources_return_noindex_404_metadata(self):
        section = CatalogSection.objects.create(store=self.store, key="local", label="Local")
        other = Store.objects.create(slug="metadata-other", name="Other")
        other_section = CatalogSection.objects.create(store=other, key="foreign", label="Foreign")
        Product.objects.create(store=other, section=other_section, slug="foreign", name="Foreign", ref="F", price="1.00")
        route = reverse("storefront-seo-metadata", args=[self.store.slug])
        response = self.client.get(route, {"path": "/products/foreign"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["robots"], "noindex, nofollow, noarchive")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")

    def test_multiple_product_slug_changes_redirect_directly_to_latest(self):
        section = CatalogSection.objects.create(store=self.store, key="decor", label="Decor")
        product = Product.objects.create(
            store=self.store, section=section, slug="first-lamp", name="Lamp",
            ref="SLUG-1", price="20.00", is_active=True,
        )
        client = APIClient(); client.force_authenticate(self.user)
        route = f"/api/catalog/store/{self.store.pk}/products/{product.pk}/"
        first = client.patch(route, {"slug": "second-lamp"}, format="json")
        second = client.patch(route, {"slug": "final-lamp"}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(
            set(ProductSlugRedirect.objects.filter(product=product).values_list("old_slug", flat=True)),
            {"first-lamp", "second-lamp"},
        )
        metadata = reverse("storefront-seo-metadata", args=[self.store.slug])
        for old in ("first-lamp", "second-lamp"):
            response = self.client.get(metadata, {"path": f"/products/{old}"})
            self.assertEqual(response.status_code, 308)
            self.assertEqual(response.json()["redirect"], "https://atelier-seo.tijarago.com/products/final-lamp")

        collision = Product.objects.create(
            store=self.store, section=section, slug="another", name="Another",
            ref="SLUG-2", price="10.00",
        )
        blocked = client.patch(
            f"/api/catalog/store/{self.store.pk}/products/{collision.pk}/",
            {"slug": "first-lamp"}, format="json",
        )
        self.assertEqual(blocked.status_code, 400)

    def test_collection_slug_history_is_tenant_scoped_and_deleted_with_resource(self):
        section = CatalogSection.objects.create(store=self.store, key="old-collection", label="Collection")
        client = APIClient(); client.force_authenticate(self.user)
        route = f"/api/catalog/store/{self.store.pk}/sections/{section.pk}/"
        changed = client.patch(route, {"key": "new-collection"}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertTrue(CatalogSectionSlugRedirect.objects.filter(store=self.store, old_slug="old-collection", section=section).exists())
        metadata = self.client.get(
            reverse("storefront-seo-metadata", args=[self.store.slug]),
            {"path": "/collections/old-collection"},
        )
        self.assertEqual(metadata.status_code, 308)
        self.assertEqual(metadata.json()["redirect"], "https://atelier-seo.tijarago.com/collections/new-collection")
        section.delete()
        self.assertFalse(CatalogSectionSlugRedirect.objects.filter(old_slug="old-collection").exists())

    def test_historical_slugs_never_resolve_across_tenants(self):
        other = Store.objects.create(slug="redirect-other", name="Redirect Other")
        other_section = CatalogSection.objects.create(store=other, key="other-section", label="Other")
        other_product = Product.objects.create(
            store=other, section=other_section, slug="current-foreign", name="Foreign",
            ref="FOREIGN-REDIRECT", price="10.00", is_active=True,
        )
        ProductSlugRedirect.objects.create(
            store=other, product=other_product, old_slug="historical-foreign",
        )
        CatalogSectionSlugRedirect.objects.create(
            store=other, section=other_section, old_slug="historical-section",
        )
        metadata = reverse("storefront-seo-metadata", args=[self.store.slug])
        for path in ("/products/historical-foreign", "/collections/historical-section"):
            response = self.client.get(metadata, {"path": path})
            self.assertEqual(response.status_code, 404)
            self.assertIsNone(response.json()["redirect"])
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow, noarchive")
