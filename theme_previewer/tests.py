from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.core.exceptions import ValidationError

from api.designs import DesignNotFound
from store.models import Store, StoreConfig
from storefront_factory.scaffold import storefront_security_headers
from .models import ThemeDemoStore
from .provisioning import provision_theme_demo
from .tickets import issue_preview_ticket, validate_preview_ticket


READY_DESIGN = {
    "key": "basic", "name": "Basic", "status": "ready", "tier": "standard",
    "tagline": "A clear storefront", "description": "A reliable commerce experience.",
}


class ThemePreviewRegistryTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Demo", slug="demo-basic")
        self.store.config.design = "basic"
        self.store.config.save(update_fields=["design"])
        self.profile = ThemeDemoStore.objects.create(
            theme_key="basic", store=self.store, public_url="https://basic.preview.tijarago.com",
            deployment_status=ThemeDemoStore.DeploymentStatus.READY, is_enabled=True,
        )

    @patch("theme_previewer.views.get_design", return_value=READY_DESIGN)
    @patch("theme_previewer.registry.get_design", return_value=READY_DESIGN)
    def test_public_detail_exposes_only_registered_ready_preview(self, _registry_design, _view_design):
        response = self.client.get(reverse("theme-preview-detail", args=["basic"]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["preview_available"])
        self.assertEqual(response.json()["demo_store_slug"], "demo-basic")
        self.assertTrue(response.json()["preview_ticket"])

        self.profile.is_enabled = False
        self.profile.save(update_fields=["is_enabled"])
        response = self.client.get(reverse("theme-preview-detail", args=["basic"]))
        self.assertEqual(response.status_code, 404)

    def test_every_registered_demo_is_read_only_even_before_publication(self):
        self.profile.is_enabled = False
        self.profile.save(update_fields=["is_enabled"])
        response = self.client.post("/api/designs/", {"email": "buyer@example.com"},
                                    HTTP_X_STORE_SLUG=self.store.slug)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "theme_demo_read_only")
        self.assertIn("design=basic", response.json()["signup_url"])

        for path in (
            "/api/orders/", "/api/payments/initiate/", "/api/auth/register/",
            "/api/catalog/products/example/reviews/", "/api/subscribe/",
        ):
            with self.subTest(path=path):
                blocked = self.client.post(path, {}, content_type="application/json",
                                           HTTP_X_STORE_SLUG=self.store.slug)
                self.assertEqual(blocked.status_code, 403)
                self.assertEqual(blocked.json()["code"], "theme_demo_read_only")

    def test_non_demo_tenant_is_not_intercepted(self):
        store = Store.objects.create(name="Merchant", slug="merchant")
        response = self.client.post("/api/designs/", {"email": "buyer@example.com"},
                                    HTTP_X_STORE_SLUG=store.slug)
        self.assertNotEqual(response.json().get("code"), "theme_demo_read_only")

    def test_preview_url_requires_https_without_credentials(self):
        self.profile.public_url = "http://basic.preview.tijarago.com"
        with self.assertRaises(ValidationError):
            self.profile.full_clean()
        self.profile.public_url = "https://user:secret@basic.preview.tijarago.com"
        with self.assertRaises(ValidationError):
            self.profile.full_clean()

    @patch("theme_previewer.views.get_design", side_effect=DesignNotFound("missing"))
    def test_unknown_theme_is_not_public(self, _get_design):
        response = self.client.get(reverse("theme-preview-detail", args=["missing"]))
        self.assertEqual(response.status_code, 404)

    def test_preview_ticket_is_bound_to_theme_and_store(self):
        ticket = issue_preview_ticket(theme_key="basic", store_slug="demo-basic")
        self.assertTrue(validate_preview_ticket(ticket, theme_key="basic", store_slug="demo-basic"))
        self.assertFalse(validate_preview_ticket(ticket, theme_key="beauty", store_slug="demo-basic"))
        self.assertFalse(validate_preview_ticket(ticket, theme_key="basic", store_slug="another-store"))
        self.assertFalse(validate_preview_ticket(f"{ticket}x", theme_key="basic", store_slug="demo-basic"))

    @override_settings(THEME_PREVIEW_ALLOWED_ORIGINS=("https://www.tijarago.com",))
    def test_demo_csp_allows_only_the_showroom(self):
        demo_headers = storefront_security_headers(SimpleNamespace(
            store_id=self.store.id, design="basic",
        ))
        values = {item["key"]: item["value"] for item in demo_headers}
        self.assertNotIn("X-Frame-Options", values)
        self.assertIn("frame-ancestors https://www.tijarago.com", values["Content-Security-Policy"])

        normal_headers = storefront_security_headers(SimpleNamespace(store_id=None, design="basic"))
        normal_values = {item["key"]: item["value"] for item in normal_headers}
        self.assertEqual(normal_values["X-Frame-Options"], "DENY")


class ThemeDemoProvisioningTests(TestCase):
    @patch("theme_previewer.provisioning.get_design", return_value=READY_DESIGN)
    def test_provisioning_is_idempotent_and_does_not_erase_public_url(self, _get_design):
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            first = provision_theme_demo("basic", public_url="https://basic.preview.tijarago.com")
            counts = (Store.objects.count(), first.store.products.count(), first.store.catalog_sections.count())
            second = provision_theme_demo("basic")
            second.refresh_from_db()
            self.assertEqual(counts, (Store.objects.count(), second.store.products.count(), second.store.catalog_sections.count()))
            self.assertEqual(second.public_url, "https://basic.preview.tijarago.com")
            self.assertFalse(second.store.members.exists())
            self.assertTrue(all(product.variant_options[0]["values"] for product in second.store.products.all()))
            self.assertEqual(second.deployment.store_id, second.store_id)
            self.assertEqual(second.deployment.design, "basic")
            self.assertEqual(second.store.storefront_requests.count(), 1)

    @patch("api.designs.get_design")
    @patch("theme_previewer.provisioning.get_design")
    def test_premium_theme_validation_uses_the_saved_store_config(self, provision_design, access_design):
        design = lambda key: {
            **READY_DESIGN, "key": key, "name": key.title(), "tier": "premium",
        }
        provision_design.side_effect = design
        access_design.side_effect = design
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            profile = provision_theme_demo("beauty")
        self.assertEqual(StoreConfig.objects.get(store=profile.store).design, "beauty")
        profile.full_clean()
