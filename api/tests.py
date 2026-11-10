"""Tests for the public storefront config endpoint and store creation
with its runtime storefront config (Option A)."""
import asyncio
import base64
import os
import tempfile
from copy import deepcopy
from unittest.mock import patch
from django.db import OperationalError
from django.db.models.deletion import ProtectedError
from datetime import timedelta
from types import SimpleNamespace

from django.test import AsyncRequestFactory, SimpleTestCase, TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import force_authenticate

from accounts.models import AuthUser, MerchantProfile
from accounts.cookies import access_cookie_name
from accounts.tokens import MERCHANT_SCOPE, issue_scoped_tokens
from catalog.models import (
    CatalogSection, Product, ProductOption, ProductOptionValue, RetailType,
    StockVariant, VariantSelection,
)
from orders.models import Order
from store.models import Store, StoreConfig, StorefrontAsset, StorefrontPage, StoreMember
from storefront_factory.deployment_state import ensure_initial_deployment_event, record_deployment_state
from storefront_factory.models import StorefrontRequest
from storefront_factory.services import InfrastructureCleanupError
from api.serializers import DesignSerializer
from api.views import RequestEventsView
from subscriptions.models import SubscriptionPayment, SubscriptionPlan, UserSubscription
from subscriptions.services import ensure_free_subscription


def _set_jwt_cookie(client, user):
    MerchantProfile.objects.get_or_create(user=user)
    if user.email_verified_at is None:
        user.email_verified_at = timezone.now()
        user.save(update_fields=["email_verified_at"])
    access, _refresh = issue_scoped_tokens(user, MERCHANT_SCOPE)
    client.cookies[access_cookie_name("owner")] = access


@override_settings(STOREFRONT_RELEASE_REVISION="")
class DesignSerializerTests(TestCase):
    def test_exposes_tier_and_production_features_to_showroom(self):
        payload = DesignSerializer({
            "key": "electronics",
            "name": "Electronics",
            "tagline": "Compare what matters",
            "description": "Specification-led commerce",
            "family": "electronics_specification",
            "business_need": "Help shoppers compare technical products.",
            "business_outcomes": [
                "Make specifications easier to assess",
                "Find the right item faster",
            ],
            "best_for": ["Consumer electronics", "Computers"],
            "journey": ["Browse", "Compare", "Buy"],
            "status": "ready",
            "screenshots": [],
            "demo_url": None,
            "tier": "premium",
            "features": ["facetedSearch", "compare"],
            "feature_maturity": {
                "compare": "production",
                "facetedSearch": "production",
            },
            "config_schema": [],
        }).data

        self.assertEqual(payload["tier"], "premium")
        self.assertEqual(payload["features"], ["facetedSearch", "compare"])
        self.assertEqual(payload["feature_maturity"]["compare"], "production")
        self.assertEqual(payload["business_outcomes"][0], "Make specifications easier to assess")

    def test_design_catalog_api_preserves_premium_contracts(self):
        response = self.client.get("/api/designs/")

        self.assertEqual(response.status_code, 200, response.content)
        designs = response.json()
        self.assertEqual(len(designs), 5)
        premium = [design for design in designs if design["tier"] == "premium"]
        self.assertEqual({design["key"] for design in premium}, {"beauty", "fashion", "electronics", "nexa"})
        self.assertTrue(all(len(design["features"]) >= 2 for design in premium))
        self.assertTrue(all(len(design["business_outcomes"]) >= 2 for design in designs))
        self.assertEqual(
            {design["key"] for design in designs if design["status"] == "ready"},
            {"basic", "beauty", "fashion", "electronics", "nexa"},
        )
        self.assertTrue(
            all(
                all(design["feature_maturity"].get(feature) == "production" for feature in design["features"])
                for design in premium
            )
        )
        self.assertIn("max-age=60", response["Cache-Control"])
        self.assertIn("s-maxage=300", response["Cache-Control"])
        self.assertIn("stale-while-revalidate=600", response["Cache-Control"])

    @patch("theme_previewer.registry.ThemeDemoStore.objects.select_related")
    def test_design_catalog_survives_unavailable_preview_table(self, select_related):
        select_related.side_effect = OperationalError("preview table is unavailable")

        response = self.client.get("/api/designs/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()), 5)
        self.assertTrue(all(item["preview_available"] is False for item in response.json()))

    def test_nexa_requires_a_premium_plan(self):
        from store.design_access import can_use_design

        self.assertFalse(can_use_design("free", "nexa"))
        self.assertTrue(can_use_design("premium", "nexa"))

    def test_atelier_is_retired_without_breaking_existing_premium_stores(self):
        from store.design_access import can_use_design, effective_design

        self.assertFalse(can_use_design("premium", "atelier"))
        self.assertEqual(effective_design("premium", "atelier"), "atelier")

    @override_settings(STOREFRONT_RELEASE_REVISION="a" * 40)
    def test_catalog_uses_the_same_pinned_source_revision_as_deployment(self):
        from api.designs import _manifest_cache

        _manifest_cache.update(data=None, ts=0, source=None)
        payload = b'{"designs":[{"key":"basic","name":"Basic","tagline":"Basic","description":"Basic","family":"basic","business_need":"Basic","best_for":["A","B"],"journey":["A","B"],"business_outcomes":["A","B"],"status":"ready","screenshots":[],"demo_url":null,"tier":"standard","features":[],"feature_maturity":{},"config_schema":[]},{"key":"variants","name":"Old","tagline":"Old","description":"Old","family":"old","business_need":"Old","best_for":["A","B"],"journey":["A","B"],"business_outcomes":["A","B"],"status":"ready","screenshots":[],"demo_url":null,"tier":"premium","features":[],"feature_maturity":{},"config_schema":[]}]}'
        with patch("api.designs.urlopen") as open_manifest:
            open_manifest.return_value.__enter__.return_value.read.return_value = payload
            response = self.client.get("/api/designs/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([design["key"] for design in response.json()], ["basic"])
        self.assertEqual(self.client.get("/api/designs/variants/").status_code, 404)
        requested_url = open_manifest.call_args.args[0].full_url
        self.assertIn("/" + "a" * 40 + "/manifest.json", requested_url)
        _manifest_cache.update(data=None, ts=0, source=None)


@override_settings(STOREFRONT_BASE_DOMAIN="alfirdaousstore.com", ALLOWED_HOSTS=["*"])
class StorefrontConfigViewTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(slug="acme", name="Acme Store")
        self.owner = AuthUser.objects.create_user(email="acme-owner@example.test", password="pass")
        MerchantProfile.objects.create(user=self.owner)
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        ensure_free_subscription(self.owner)
        self.config, _ = StoreConfig.objects.get_or_create(store=self.store)
        self.config.design = "basic"
        self.config.contact_email = "hello@acme.test"
        self.config.brand_color = "#1F8A8C"
        self.config.currency = "MAD"
        self.config.shipping_fee = 25.0
        self.config.free_shipping_enabled = True
        self.config.free_shipping_threshold = 700
        self.config.free_shipping_currency = "MAD"
        self.config.support_phone = "+212612345678"
        self.config.whatsapp_phone = "+212698765432"
        self.config.social_links = {"instagram": "https://www.instagram.com/acme"}
        self.config.appearance = {
            "schema_version": 1,
            "typography": {"body_font": "humanist", "heading_font": "editorial", "base_size": "large", "heading_scale": "large"},
            "header": {"layout": "logo_center", "sticky": "none", "show_account": False, "show_language": True},
            "style": {"button_shape": "pill", "card_shape": "soft", "card_padding": "compact", "card_alignment": "start", "card_hover": "lift", "content_width": "wide"},
            "footer": {"show_contact": True, "show_social_links": False, "copyright": {"en": "Acme Ltd.", "fr": "Acme SARL", "ar": "شركة أكمي"}},
        }
        self.config.homepage_product_source = "featured"
        self.config.homepage_product_limit = 8
        self.config.save(update_fields=[
            "design", "contact_email", "brand_color", "currency", "shipping_fee",
            "free_shipping_enabled", "free_shipping_threshold", "free_shipping_currency",
            "support_phone", "whatsapp_phone", "social_links", "homepage_product_source",
            "homepage_product_limit", "appearance",
        ])

    def test_expired_owner_pack_returns_storefront_maintenance_contract(self):
        UserSubscription.objects.filter(user=self.owner, status="active").update(
            status="expired", current_period_end=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["code"], "storefront_temporarily_unavailable")
        self.assertEqual(response.json()["storeName"], "Acme Store")
        self.assertEqual(response["Cache-Control"], "no-store")

        catalog = self.client.get("/api/catalog/products/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(catalog.status_code, 402)
        self.assertEqual(catalog.json()["code"], "storefront_temporarily_unavailable")

    def test_expired_merchant_is_blocked_outside_billing_and_auth(self):
        UserSubscription.objects.filter(user=self.owner, status="active").update(
            status="expired", current_period_end=timezone.now() - timedelta(seconds=1),
        )
        _set_jwt_cookie(self.client, self.owner)
        blocked = self.client.get(f"/api/store/{self.store.pk}/config/")
        billing = self.client.get("/api/subscriptions/status")
        self.assertEqual(blocked.status_code, 402)
        self.assertEqual(blocked.json()["code"], "subscription_required")
        self.assertEqual(billing.status_code, 200)
        self.assertFalse(billing.json()["access"]["allowed"])

    def test_resolves_store_via_x_store_slug_header(self):
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["storeName"], "Acme Store")
        self.assertEqual(data["slug"], "acme")
        self.assertEqual(data["design"], "basic")
        self.assertEqual(data["contactEmail"], "hello@acme.test")
        self.assertEqual(data["currency"], "MAD")
        self.assertEqual(data["defaultLocale"], "ar")
        self.assertEqual(data["shippingFee"], 25.0)
        self.assertEqual(data["freeShippingThreshold"], 700.0)
        self.assertEqual(data["phone"], "+212612345678")
        self.assertEqual(data["whatsappPhone"], "+212698765432")
        self.assertEqual(data["socialLinks"], {"instagram": "https://www.instagram.com/acme"})
        self.assertEqual(data["appearance"]["typography"], self.config.appearance["typography"])
        self.assertEqual(data["appearance"]["colors"], {
            "theme_primary": "", "theme_secondary": "", "theme_muted": "",
            "page": "", "header": "", "header_text": "", "navigation": "", "navigation_text": "", "announcement": "", "hero": "",
            "section": "", "card": "", "discount_badge": "", "discount_badge_text": "", "newsletter": "", "footer": "",
        })
        self.assertEqual(data["appearance"]["header"]["width"], "contained")
        self.assertTrue(data["appearance"]["header"]["show_search"])

    def test_saved_store_locale_is_returned_to_the_storefront(self):
        self.config.default_locale = "fr"
        self.config.save(update_fields=["default_locale"])

        data = self.client.get(
            "/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug,
        ).json()

        self.assertEqual(data["defaultLocale"], "fr")
        self.assertTrue(data["appearance"]["header"]["show_cart"])
        self.assertEqual(data["homepage"], {"productSource": "featured", "productLimit": 8})
        self.assertEqual(data["pages"]["home"]["revision"], 1)
        self.assertTrue(any(section["type"] == "products" for section in data["pages"]["home"]["sections"]))
        self.assertIsNone(data["logoUrl"])
        self.assertEqual(data["domain"], "https://acme.alfirdaousstore.com")
        # palette derived from brand_color
        self.assertEqual(data["palette"]["brand"], "#1F8A8C")
        self.assertEqual(len(data["palette"]), 7)

    def test_formatted_page_copy_is_plain_for_old_bundles_and_rich_for_new_bundles(self):
        # A published About section from the rich editor must never show literal
        # markup in a storefront bundle built before formatted sections existed.
        self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        page = StorefrontPage.objects.get(store=self.store, page_key="about")
        sections = deepcopy(page.published_sections)
        story = next(section for section in sections if section["type"] == "image_text")
        story["settings"]["body"]["ar"] = "<p>😊 hey <strong>guys</strong></p>"
        page.published_sections = sections
        page.save(update_fields=["published_sections"])

        legacy = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme").json()
        self.assertEqual(legacy["pages"]["about"]["sections"][1]["settings"]["body"]["ar"], "😊 hey guys")
        rich = self.client.get("/api/storefront/config/?rich_text=1", HTTP_X_STORE_SLUG="acme").json()
        self.assertEqual(rich["pages"]["about"]["sections"][1]["settings"]["body"]["ar"], "<p>😊 hey <strong>guys</strong></p>")
        page.refresh_from_db()
        self.assertEqual(page.published_sections[1]["settings"]["body"]["ar"], "<p>😊 hey <strong>guys</strong></p>")

    def test_resolves_store_via_subdomain(self):
        resp = self.client.get("/api/storefront/config/", HTTP_HOST="acme.alfirdaousstore.com")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["slug"], "acme")

    def test_alternate_api_host_uses_explicit_store_header(self):
        response = self.client.get("/api/storefront/config/", HTTP_HOST="qa-api.ngrok-free.app", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["slug"], "acme")

    def test_unrelated_domain_is_not_inferred_as_a_store(self):
        response = self.client.get("/api/storefront/config/", HTTP_HOST="acme.unrelated.example")
        self.assertEqual(response.status_code, 404)

    def test_custom_domain_override_wins(self):
        self.config.domain = "https://shop.acme.test"
        self.config.save(update_fields=["domain"])
        from store.models import StorefrontDomain
        StorefrontDomain.objects.create(
            store=self.store,
            hostname="shop.acme.test",
            display_hostname="shop.acme.test",
            kind=StorefrontDomain.Kind.CUSTOM,
            status=StorefrontDomain.Status.ACTIVE,
            certificate_status=StorefrontDomain.CertificateStatus.VALID,
            ownership_verified_at=timezone.now(),
            provider_verified=True,
            health_verified_at=timezone.now(),
            is_primary=True,
        )
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.json()["domain"], "https://shop.acme.test")

    def test_empty_store_gets_default_sections(self):
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.json()["sections"], [
            {"key": "men", "label": "Men", "icon": "FaTshirt:fa"},
            {"key": "women", "label": "Women", "icon": "FaBagShopping:fa6"},
            {"key": "accessories", "label": "Accessories", "icon": "FaBoxOpen:fa"},
        ])

    def test_unknown_store_404(self):
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="nope")
        self.assertEqual(resp.status_code, 404)

    def test_cache_headers(self):
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertIn("Cache-Control", resp.headers)
        self.assertIn("s-maxage", resp.headers["Cache-Control"])

    def test_appearance_patch_is_normalized_and_rejects_unknown_values(self):
        _set_jwt_cookie(self.client, self.owner)

        accepted = self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"appearance": {"header": {"layout": "logo_center", "width": "full", "logo_size": "large", "nav_alignment": "start", "show_search": False, "show_cart": True, "show_categories": True, "show_about": False, "show_contact": True}, "style": {"button_shape": "pill"}}},
            content_type="application/json",
        )
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(accepted.json()["appearance"]["header"]["layout"], "logo_center")
        self.assertEqual(accepted.json()["appearance"]["header"]["width"], "full")
        self.assertEqual(accepted.json()["appearance"]["header"]["logo_size"], "large")
        self.assertEqual(accepted.json()["appearance"]["header"]["nav_alignment"], "start")
        self.assertFalse(accepted.json()["appearance"]["header"]["show_search"])
        self.assertTrue(accepted.json()["appearance"]["header"]["show_categories"])
        self.assertFalse(accepted.json()["appearance"]["header"]["show_about"])
        self.assertTrue(accepted.json()["appearance"]["header"]["show_contact"])
        self.assertEqual(accepted.json()["appearance"]["typography"]["body_font"], "system")
        self.assertEqual(accepted.json()["appearance"]["colors"]["header"], "")

        colored = self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"appearance": {"colors": {"page": "#fafafa", "header_text": "#102030", "navigation": "#223344", "navigation_text": "#F8FAFC", "discount_badge": "#FDE68A", "discount_badge_text": "#422006", "footer": "#123456"}}},
            content_type="application/json",
        )
        self.assertEqual(colored.status_code, 200, colored.content)
        self.assertEqual(colored.json()["appearance"]["colors"]["page"], "#FAFAFA")
        self.assertEqual(colored.json()["appearance"]["colors"]["footer"], "#123456")
        self.assertEqual(colored.json()["appearance"]["colors"]["header_text"], "#102030")
        self.assertEqual(colored.json()["appearance"]["colors"]["navigation"], "#223344")
        self.assertEqual(colored.json()["appearance"]["colors"]["navigation_text"], "#F8FAFC")
        self.assertEqual(colored.json()["appearance"]["colors"]["discount_badge"], "#FDE68A")
        self.assertEqual(colored.json()["appearance"]["colors"]["discount_badge_text"], "#422006")

        invalid_color = self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"appearance": {"colors": {"page": "red"}}},
            content_type="application/json",
        )
        self.assertEqual(invalid_color.status_code, 400, invalid_color.content)

        rejected = self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"appearance": {"header": {"layout": "execute-script"}}},
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertIn("appearance", rejected.json())

    def test_public_no_auth_required(self):
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)

    def test_free_plan_cannot_render_inconsistent_premium_design(self):
        StoreConfig.objects.filter(pk=self.config.pk).update(plan="free", design="beauty")
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["design"], "basic")

    def test_premium_plan_can_render_premium_design(self):
        UserSubscription.objects.filter(user=self.owner).update(status=UserSubscription.Status.CANCELED)
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.owner, plan=plan, status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )
        # The denormalized cache starts stale in the restrictive direction;
        # runtime truth must still grant the active subscriber's design.
        StoreConfig.objects.filter(pk=self.config.pk).update(plan="free", design="beauty")
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["design"], "beauty")
        self.config.refresh_from_db()
        self.assertEqual(self.config.plan, StoreConfig.Plans.PREMIUM)

    def test_standard_store_keeps_basic_design(self):
        StoreConfig.objects.filter(pk=self.config.pk).update(plan="free", design="basic")
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["design"], "basic")

    def test_existing_premium_store_keeps_legacy_design(self):
        UserSubscription.objects.filter(user=self.owner).update(status=UserSubscription.Status.CANCELED)
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.owner, plan=plan, status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )
        StoreConfig.objects.filter(pk=self.config.pk).update(plan="free", design="story")
        resp = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG="acme")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["design"], "story")


@override_settings(ALLOWED_HOSTS=["*"])
class StorefrontPageEditorTests(TestCase):
    def slideshow(self):
        return {"id": "campaign", "type": "slideshow", "enabled": True, "settings": {
            "autoplay": False, "interval": 7, "slides": [{
                "id": "summer", "title": {"en": "Summer", "fr": "Été", "ar": "الصيف"},
                "body": {}, "alt": {}, "cta_label": {"en": "Shop"}, "cta_url": "/collections/summer",
                "image_url": "https://example.test/desktop.webp", "mobile_image_url": "https://example.test/mobile.webp",
            }],
        }}

    def test_slideshow_stays_private_until_publication_and_preserves_mobile_copy(self):
        section = self.slideshow()
        response = self.client.patch(self.detail_url, {"draft_sections": [section]}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        before = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug).json()
        self.assertNotIn("slideshow", [s["type"] for s in before["pages"]["home"]["sections"]])
        self.assertEqual(self.client.post(f"{self.detail_url}publish/").status_code, 200)
        after = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug).json()
        self.assertEqual(after["pages"]["home"]["sections"], [section])

    def test_theme_feature_uses_existing_draft_publish_boundary(self):
        feature = {
            "id": "premium-routine", "type": "theme_feature", "enabled": True,
            "settings": {
                "title": {"en": "Build a routine", "fr": "Créer une routine", "ar": "ابن روتينك"},
                "body": {"en": "Products from this store"},
                "kind": "beauty", "limit": 4,
            },
        }
        response = self.client.patch(
            self.detail_url, {"draft_sections": [feature]}, content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        public_before = self.client.get(
            "/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug,
        ).json()
        self.assertNotIn(
            "theme_feature", [section["type"] for section in public_before["pages"]["home"]["sections"]],
        )
        self.assertEqual(self.client.post(f"{self.detail_url}publish/").status_code, 200)
        public_after = self.client.get(
            "/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug,
        ).json()
        self.assertEqual(public_after["pages"]["home"]["sections"], [feature])

        invalid = deepcopy(feature)
        invalid["settings"]["kind"] = "unsupported"
        rejected = self.client.patch(
            self.detail_url, {"draft_sections": [invalid]}, content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.content)

    def test_slideshow_rejects_unsafe_links_unbounded_data_and_invalid_controls(self):
        for field, value in [("cta_url", "javascript:alert(1)"), ("cta_url", "//evil.test"), ("image_url", "http://example.test/a.png"), ("title", {"en": "x" * 181})]:
            section = self.slideshow()
            section["settings"]["slides"][0][field] = value
            with self.subTest(field=field, value=value):
                response = self.client.patch(self.detail_url, {"draft_sections": [section]}, content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)
        for field, value in [("slides", []), ("slides", self.slideshow()["settings"]["slides"] * 7), ("slides", self.slideshow()["settings"]["slides"] * 2), ("autoplay", "true"), ("interval", True), ("interval", 1), ("interval", 16)]:
            section = self.slideshow()
            section["settings"][field] = value
            with self.subTest(field=field):
                response = self.client.patch(self.detail_url, {"draft_sections": [section]}, content_type="application/json")
                self.assertEqual(response.status_code, 400, response.content)

    def setUp(self):
        self.user = AuthUser.objects.create_user(
            email="owner-page@example.test",
            password="MerchantPass123!",
        )
        _set_jwt_cookie(self.client, self.user)
        self.store = Store.objects.create(slug="page-shop", name="Page Shop")
        StoreMember.objects.create(user=self.user, store=self.store, role=StoreMember.Roles.OWNER)
        self.page = StorefrontPage.objects.get(store=self.store, page_key="home")
        self.detail_url = f"/api/store/{self.store.pk}/pages/home/"

    def test_new_store_has_a_complete_ready_homepage(self):
        response = self.client.get(self.detail_url)

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        enabled = [section["type"] for section in payload["published_sections"] if section["enabled"]]
        self.assertEqual(enabled, ["hero", "collections", "products", "newsletter"])
        self.assertEqual(payload["draft_sections"], payload["published_sections"])

    def test_page_library_contains_commerce_account_and_content_pages(self):
        response = self.client.get(f"/api/store/{self.store.pk}/pages/")

        self.assertEqual(response.status_code, 200, response.content)
        pages = {page["page_key"]: page for page in response.json()}
        self.assertTrue({"home", "catalog", "product", "about", "contact", "account", "login", "privacy", "terms"} <= set(pages))
        self.assertTrue(pages["home"]["is_protected"])
        self.assertTrue(pages["product"]["is_protected"])
        self.assertEqual(
            [section["type"] for section in pages["product"]["draft_sections"]],
            ["product_main", "product_assurances", "related_products"],
        )
        self.assertTrue(pages["account"]["is_protected"])
        self.assertFalse(pages["about"]["is_protected"])

    def test_product_purchase_section_cannot_be_removed_or_disabled(self):
        product_page = StorefrontPage.objects.get(store=self.store, page_key="product")
        detail_url = f"/api/store/{self.store.pk}/pages/product/"
        without_purchase = [
            section for section in product_page.draft_sections
            if section["type"] != "product_main"
        ]

        response = self.client.patch(
            detail_url,
            {"draft_sections": without_purchase},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("essential product purchase section", str(response.json()))

    def test_custom_pages_can_be_created_but_system_pages_cannot_be_deleted(self):
        create_response = self.client.post(
            f"/api/store/{self.store.pk}/pages/",
            {
                "title": {"en": "Size guide", "fr": "Guide des tailles", "ar": "دليل المقاسات"},
                "route": "size-guide",
                "show_in_navigation": True,
            },
            content_type="application/json",
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        self.assertEqual(create_response.json()["page_key"], "size-guide")
        self.assertEqual(
            self.client.delete(f"/api/store/{self.store.pk}/pages/size-guide/").status_code,
            204,
        )
        self.assertEqual(self.client.delete(self.detail_url).status_code, 400)

    def test_draft_is_private_until_published(self):
        draft = deepcopy(self.page.draft_sections)
        hero = next(section for section in draft if section["type"] == "hero")
        hero["settings"]["title"]["en"] = "A merchant-controlled headline"

        save_response = self.client.patch(
            self.detail_url,
            {"draft_sections": draft},
            content_type="application/json",
        )
        self.assertEqual(save_response.status_code, 200, save_response.content)
        public_before = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug).json()
        public_hero_before = next(
            section for section in public_before["pages"]["home"]["sections"] if section["type"] == "hero"
        )
        self.assertEqual(public_hero_before["settings"]["title"]["en"], "")

        publish_response = self.client.post(f"{self.detail_url}publish/")
        self.assertEqual(publish_response.status_code, 200, publish_response.content)
        self.assertEqual(publish_response.json()["revision"], 2)
        public_after = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug).json()
        public_hero_after = next(
            section for section in public_after["pages"]["home"]["sections"] if section["type"] == "hero"
        )
        self.assertEqual(public_hero_after["settings"]["title"]["en"], "A merchant-controlled headline")

    def test_rejects_unsafe_or_unknown_section_configuration(self):
        draft = deepcopy(self.page.draft_sections)
        hero = next(section for section in draft if section["type"] == "hero")
        hero["settings"]["image_url"] = "http://insecure.example.test/hero.jpg"

        response = self.client.patch(
            self.detail_url,
            {"draft_sections": draft},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("HTTPS", str(response.json()))

    def test_existing_merchandising_settings_remain_in_sync(self):
        response = self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"homepage_product_source": "newest", "homepage_product_limit": 12},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        page = StorefrontPage.objects.get(pk=self.page.pk)
        for document in (page.draft_sections, page.published_sections):
            products = next(section for section in document if section["type"] == "products")
            self.assertEqual(products["settings"]["source"], "newest")
            self.assertEqual(products["settings"]["limit"], 12)

    def test_publishing_product_section_updates_legacy_runtime_setting(self):
        draft = deepcopy(self.page.draft_sections)
        products = next(section for section in draft if section["type"] == "products")
        products["settings"].update({"source": "discounted", "limit": 16})
        self.client.patch(self.detail_url, {"draft_sections": draft}, content_type="application/json")

        response = self.client.post(f"{self.detail_url}publish/")

        self.assertEqual(response.status_code, 200, response.content)
        config = StoreConfig.objects.get(store=self.store)
        self.assertEqual(config.homepage_product_source, "discounted")
        self.assertEqual(config.homepage_product_limit, 16)

    def test_storefront_image_upload_is_scoped_validated_and_cleaned_up(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                f"/api/store/{self.store.pk}/storefront-assets/",
                {"image": SimpleUploadedFile("hero.png", png, content_type="image/png")},
            )
            self.assertEqual(response.status_code, 201, response.content)
            asset = StorefrontAsset.objects.get(pk=response.json()["id"])
            stored_path = asset.image.path
            self.assertTrue(os.path.exists(stored_path))

            asset.delete()

            self.assertFalse(os.path.exists(stored_path))

    def test_store_logo_upload_is_persisted_and_exposed_to_the_storefront(self):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                f"/api/store/{self.store.pk}/logo/",
                {"logo": SimpleUploadedFile("brand.png", png, content_type="image/png")},
            )

            self.assertEqual(response.status_code, 200, response.content)
            self.assertTrue(response.json()["logo_url"].startswith("http://testserver/"))
            self.store.refresh_from_db()
            self.assertTrue(self.store.logo.name.startswith(f"store-logos/{self.store.slug}/"))
            self.assertTrue(os.path.exists(self.store.logo.path))

            public = self.client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug)
            self.assertEqual(public.status_code, 200, public.content)
            self.assertIn("/media/store-logos/", public.json()["logoUrl"])

    def test_non_member_cannot_read_or_publish_page(self):
        outsider = AuthUser.objects.create_user(email="outsider-page@example.test", password="MerchantPass123!")
        _set_jwt_cookie(self.client, outsider)

        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertEqual(self.client.post(f"{self.detail_url}publish/").status_code, 404)

@override_settings(STOREFRONT_BASE_DOMAIN="alfirdaousstore.com")
class StoreCreationConfigTests(TestCase):
    def setUp(self):
        self.user = AuthUser.objects.create_user(email="merchant@test.local", password="secret12345")
        _set_jwt_cookie(self.client, self.user)

    def test_store_address_availability_is_neutral_and_global(self):
        Store.objects.create(name="Existing display name", slug="existing-address")

        available = self.client.get("/api/store/availability/?slug=fresh-address")
        taken = self.client.get("/api/store/availability/?slug=existing-address")
        invalid = self.client.get("/api/store/availability/?slug=not%20valid")

        self.assertEqual(available.status_code, 200)
        self.assertEqual(available.json(), {
            "slug": "fresh-address", "available": True, "reason": "available",
        })
        self.assertEqual(taken.status_code, 200)
        self.assertEqual(taken.json(), {
            "slug": "existing-address", "available": False, "reason": "taken",
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.json()["reason"], "invalid")

    def test_duplicate_store_address_returns_consistent_conflict(self):
        Store.objects.create(name="Existing", slug="duplicate-address")

        response = self.client.post(
            "/api/store/",
            {"name": "Another display name", "slug": "duplicate-address"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "store_address_taken")
        self.assertEqual(response.json()["detail"], "A store with this address already exists.")

    def test_create_store_with_storefront_config(self):

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/api/store/",
                {
                    "name": "Acme Store",
                    "slug": "acme",
                    "config": {
                        "design": "basic",
                        "contact_email": "hello@acme.test",
                        "support_phone": "+212612345678",
                        "whatsapp_phone": "+212698765432",
                        "social_links": {"instagram": "https://www.instagram.com/acme"},
                        "homepage_product_source": "newest",
                        "homepage_product_limit": 12,
                        "brand_color": "#123456",
                        "currency": "USD",
                    },
                },
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["slug"], "acme")
        self.assertIsNone(data["storefront_url"])
        self.assertEqual(data["storefront_status"], "draft")
        self.assertEqual(data["storefront_progress"], 0)

        store = Store.objects.get(slug="acme")
        config = store.config
        self.assertEqual(config.design, "basic")
        self.assertEqual(config.contact_email, "hello@acme.test")
        self.assertEqual(str(config.support_phone), "+212612345678")
        self.assertEqual(str(config.whatsapp_phone), "+212698765432")
        self.assertEqual(config.social_links, {"instagram": "https://www.instagram.com/acme"})
        self.assertEqual(config.homepage_product_source, "newest")
        self.assertEqual(config.homepage_product_limit, 12)
        self.assertEqual(config.brand_color, "#123456")
        self.assertEqual(config.currency, "USD")
        self.assertEqual(config.plan, StoreConfig.Plans.FREE)
        self.assertEqual(data["plan"], "free")
        self.assertTrue(StoreMember.objects.filter(store=store, user=self.user).exists())
        self.assertEqual(store.retail_type.key, "general")
        self.assertEqual(list(store.catalog_sections.values_list("key", flat=True)), ["products"])
        deployment = store.storefront_requests.get()
        self.assertEqual(data["storefront_request_id"], deployment.id)
        self.assertEqual(deployment.status, StorefrontRequest.STATUS_PENDING)
        self.assertEqual(deployment.deploy_status, StorefrontRequest.DEPLOY_NOT_DEPLOYED)
        self.assertEqual(deployment.domain, "https://acme.alfirdaousstore.com")
        self.assertIsNone(deployment.task_id)

    def test_create_store_without_config_uses_defaults(self):

        resp = self.client.post(
            "/api/store/",
            {"name": "Plain Store", "slug": "plain"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        config = Store.objects.get(slug="plain").config
        self.assertEqual(config.design, "basic")
        self.assertEqual(config.currency, "MAD")

    def test_store_list_exposes_the_owner_analytics_level_to_team_members(self):
        store = Store.objects.create(slug="insight-store", name="Insight Store")
        StoreMember.objects.create(store=store, user=self.user, role=StoreMember.Roles.OWNER)
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"analytics.level": "advanced"},
        )
        teammate = AuthUser.objects.create_user(
            email="analytics-team@test.local",
            password="secret12345",
        )
        StoreMember.objects.create(store=store, user=teammate, role=StoreMember.Roles.ADMIN)
        _set_jwt_cookie(self.client, teammate)

        response = self.client.get("/api/store/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["analytics_level"], "advanced")

    def test_free_plan_allows_one_store_and_guides_upgrade_after_limit(self):
        first = self.client.post(
            "/api/store/", {"name": "First Store", "slug": "first-store"},
            content_type="application/json",
        )
        second = self.client.post(
            "/api/store/", {"name": "Second Store", "slug": "second-store"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 403, second.content)
        self.assertEqual(second.json()["code"], "store_limit_reached")
        self.assertEqual(second.json()["limit"], 1)
        self.assertEqual(second.json()["used"], 1)
        self.assertEqual(Store.objects.filter(members__user=self.user).count(), 1)

    def test_paid_plan_store_allowance_is_enforced_from_subscription_snapshot(self):
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"stores.max_count": 3, "designs.premium": True},
        )

        for index in range(3):
            response = self.client.post(
                "/api/store/",
                {"name": f"Growth Store {index}", "slug": f"growth-store-{index}"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 201, response.content)

        blocked = self.client.post(
            "/api/store/", {"name": "Fourth Store", "slug": "fourth-store"},
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 403, blocked.content)
        self.assertEqual(blocked.json()["code"], "store_limit_reached")
        self.assertEqual(blocked.json()["limit"], 3)
        self.assertEqual(Store.objects.filter(members__user=self.user).count(), 3)

    def test_free_plan_rejects_premium_design_during_store_creation(self):
        resp = self.client.post(
            "/api/store/",
            {
                "name": "Premium Attempt",
                "slug": "premium-attempt",
                "config": {"design": "beauty"},
            },
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("current plan", str(resp.json()))
        self.assertFalse(Store.objects.filter(slug="premium-attempt").exists())

    def test_account_premium_subscription_allows_premium_design_at_creation(self):
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"stores.max_count": 3, "designs.premium": True},
        )

        response = self.client.post(
            "/api/store/",
            {
                "name": "Premium Store",
                "slug": "premium-store",
                "config": {"design": "beauty", "brand_color": "#654321"},
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        store = Store.objects.get(slug="premium-store")
        self.assertEqual(store.config.plan, StoreConfig.Plans.PREMIUM)
        self.assertEqual(store.config.design, "beauty")
        self.assertEqual(store.storefront_requests.get().design, "beauty")

    def test_unknown_design_is_rejected_before_store_is_created(self):
        response = self.client.post(
            "/api/store/",
            {"name": "Unknown", "slug": "unknown-design", "config": {"design": "missing"}},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Store.objects.filter(slug="unknown-design").exists())

    def test_building_family_is_not_selectable_or_created(self):
        with patch("api.designs.get_design", return_value={"key": "future", "status": "building", "tier": "premium"}):
            response = self.client.post(
                "/api/store/",
                {
                    "name": "Unfinished Family",
                    "slug": "unfinished-family",
                    "config": {"design": "future"},
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("still being prepared", str(response.json()))
        self.assertFalse(Store.objects.filter(slug="unfinished-family").exists())

    def test_free_plan_rejects_premium_design_config_update(self):
        store = Store.objects.create(slug="free-config", name="Free Config")
        StoreMember.objects.create(store=store, user=self.user, role=StoreMember.Roles.OWNER)

        resp = self.client.patch(
            f"/api/store/{store.id}/config/",
            {"design": "beauty"},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Premium plan", str(resp.json()))

    def test_premium_plan_accepts_premium_design_config_update(self):
        store = Store.objects.create(slug="premium-config", name="Premium Config")
        StoreMember.objects.create(store=store, user=self.user, role=StoreMember.Roles.OWNER)
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"designs.premium": True},
        )

        resp = self.client.patch(
            f"/api/store/{store.id}/config/",
            {"design": "beauty"},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content)
        store.config.refresh_from_db()
        self.assertEqual(store.config.design, "beauty")

    def test_store_slug_must_be_safe_for_domain_and_scaffold(self):
        for slug in ("-bad", "bad-", "bad--slug", "bad_slug", "24-store"):
            with self.subTest(slug=slug):
                resp = self.client.post(
                    "/api/store/",
                    {"name": "Invalid Slug", "slug": slug},
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400, resp.content)

    def test_selected_retail_type_seeds_only_its_catalog(self):
        electronics = RetailType.objects.get(key="electronics")
        resp = self.client.post(
            "/api/store/",
            {"name": "Devices", "slug": "devices", "retail_type": electronics.id},
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        store = Store.objects.get(slug="devices")
        self.assertEqual(store.retail_type, electronics)
        self.assertEqual(list(store.catalog_sections.values_list("key", flat=True)), ["electronics"])
        self.assertEqual(
            set(store.catalog_sections.get().attribute_defs.values_list("key", flat=True)),
            {"storage", "color", "brand", "warranty"},
        )

    def test_storefront_url_is_only_shown_after_verified_live_state(self):

        self.client.post(
            "/api/store/",
            {"name": "Acme Store", "slug": "acme"},
            content_type="application/json",
        )
        resp = self.client.get("/api/store/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()[0]["storefront_url"])
        self.assertEqual(resp.json()[0]["storefront_status"], "draft")

        deployment = Store.objects.get(slug="acme").storefront_requests.get()
        deployment.deployed_url = "https://acme.alfirdaousstore.com"
        deployment.save(update_fields=["deployed_url"])
        record_deployment_state(deployment.pk, StorefrontRequest.PIPELINE_LIVE, message="deployment.live")

        live = self.client.get("/api/store/").json()[0]
        self.assertEqual(live["storefront_url"], "https://acme.alfirdaousstore.com")
        self.assertEqual(live["storefront_status"], "live")

    def test_failed_update_keeps_the_last_verified_storefront_accessible(self):
        store = Store.objects.create(slug="stable-live", name="Stable Live")
        StoreMember.objects.create(user=self.user, store=store, role=StoreMember.Roles.OWNER)
        StorefrontRequest.objects.create(
            store=store,
            design="basic",
            name=store.name,
            slug=store.slug,
            status=StorefrontRequest.STATUS_FAILED,
            deploy_status=StorefrontRequest.DEPLOY_FAILED,
            pipeline_state=StorefrontRequest.PIPELINE_FAILED,
            operation=StorefrontRequest.OPERATION_UPDATE,
            target_revision="b" * 40,
            deployed_revision="a" * 40,
            last_known_good_revision="a" * 40,
            last_known_good_deployment_id="dpl_stable",
            deployed_url="https://stable-live.alfirdaousstore.com",
        )

        response = self.client.get("/api/store/")

        self.assertEqual(response.status_code, 200, response.content)
        payload = next(item for item in response.json() if item["id"] == store.id)
        self.assertEqual(payload["storefront_status"], "live")
        self.assertEqual(payload["storefront_release_status"], "update_failed")
        self.assertEqual(
            payload["storefront_url"],
            "https://stable-live.alfirdaousstore.com",
        )

    @patch("storefront_factory.services.get_deployment_state")
    def test_store_list_does_not_wait_for_provider_reconciliation(self, provider_status):
        store = Store.objects.create(slug="already-live", name="Already Live")
        StoreMember.objects.create(user=self.user, store=store, role=StoreMember.Roles.OWNER)
        deployment = StorefrontRequest.objects.create(
            store=store,
            design="basic",
            name=store.name,
            slug=store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            pipeline_state=StorefrontRequest.PIPELINE_BUILDING,
            progress=82,
            vercel_deployment_id="dpl_already_live",
            deployed_url="https://already-live.alfirdaousstore.com",
        )

        response = self.client.get("/api/store/")

        self.assertEqual(response.status_code, 200, response.content)
        payload = next(item for item in response.json() if item["id"] == store.id)
        self.assertEqual(payload["storefront_status"], "preparing")
        self.assertEqual(payload["storefront_progress"], 82)
        provider_status.assert_not_called()


class StoreDeletionTests(TestCase):
    def setUp(self):
        self.cleanup_patcher = patch(
            "storefront_factory.services.cleanup_storefront_infrastructure"
        )
        self.cleanup = self.cleanup_patcher.start()
        self.addCleanup(self.cleanup_patcher.stop)
        self.user = AuthUser.objects.create_user(
            email="delete-owner@test.local", password="secret12345",
        )
        _set_jwt_cookie(self.client, self.user)
        self.store = Store.objects.create(slug="delete-me", name="Delete Me")
        StoreMember.objects.create(
            user=self.user, store=self.store, role=StoreMember.Roles.OWNER,
        )

    def delete(self, **overrides):
        payload = {
            "store_name": "delete-me",
            "confirmation": "delete my store",
            **overrides,
        }
        return self.client.delete(
            f"/api/store/{self.store.id}/",
            payload,
            content_type="application/json",
        )

    def test_requires_exact_store_name_and_phrase(self):
        wrong_phrase = self.delete(confirmation="delete-me")
        self.assertEqual(wrong_phrase.status_code, 400, wrong_phrase.content)
        self.assertEqual(wrong_phrase.json()["code"], "store_delete_confirmation_failed")
        self.assertNotIn("delete my store", str(wrong_phrase.json()))
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

        wrong_name = self.delete(store_name="Delete Me")
        self.assertEqual(wrong_name.status_code, 400, wrong_name.content)
        self.assertEqual(wrong_name.json()["code"], "store_delete_confirmation_failed")
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

    def test_only_owner_can_delete_store(self):
        self.store.members.filter(user=self.user).update(role=StoreMember.Roles.ADMIN)
        response = self.delete()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

    def test_active_orders_block_deletion(self):
        Order.objects.create(store=self.store, status=Order.Status.CONFIRMED)
        response = self.delete()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "store_has_active_orders")
        self.assertEqual(response.json()["active_orders"], 1)
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

    def test_running_deployment_blocks_deletion(self):
        StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
        )
        response = self.delete()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "store_deployment_active")
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())
        self.cleanup.assert_not_called()

    def test_abandoned_queued_deployment_does_not_block_deletion(self):
        StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            task_id="d9bb1981-d5b8-460c-b5ce-9c29d2a97933",
            last_heartbeat_at=timezone.now() - timedelta(minutes=7),
        )

        response = self.delete()

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Store.objects.filter(pk=self.store.pk).exists())

    def test_abandoned_pre_dispatch_deployment_without_task_does_not_block_deletion(self):
        deployment = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
        )
        StorefrontRequest.objects.filter(pk=deployment.pk).update(
            last_heartbeat_at=timezone.now() - timedelta(minutes=7),
        )

        response = self.delete()

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Store.objects.filter(pk=self.store.pk).exists())

    def test_hosted_infrastructure_is_removed_before_database_data(self):
        deployment = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            vercel_project_id="prj_delete_me",
            deployed_url="https://delete-me.alfirdaousstore.com",
            domain="https://delete-me.alfirdaousstore.com",
        )

        response = self.delete()

        self.assertEqual(response.status_code, 204, response.content)
        cleanup_deployments = self.cleanup.call_args.args[0]
        self.assertEqual([item.pk for item in cleanup_deployments], [deployment.pk])
        self.assertFalse(Store.objects.filter(pk=self.store.pk).exists())

    def test_provider_cleanup_failure_preserves_store_for_retry(self):
        StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            vercel_project_id="prj_delete_me",
            deployed_url="https://delete-me.alfirdaousstore.com",
            domain="https://delete-me.alfirdaousstore.com",
        )
        self.cleanup.side_effect = InfrastructureCleanupError("Cloudflare unavailable")

        response = self.delete()

        self.assertEqual(response.status_code, 502, response.content)
        self.assertEqual(response.json()["code"], "store_cleanup_failed")
        self.assertNotIn("Cloudflare", str(response.json()))
        self.assertTrue(response.json()["infrastructure_cleanup_failed"])
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

    @patch("store.views._preflight_store_deletion")
    def test_protected_data_stops_before_external_cleanup(self, preflight):
        preflight.side_effect = ProtectedError("protected", set())

        response = self.delete()

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "store_delete_blocked")
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())
        self.cleanup.assert_not_called()

    def test_confirmed_owner_can_permanently_delete_store(self):
        response = self.delete()
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Store.objects.filter(pk=self.store.pk).exists())
        self.assertFalse(StoreMember.objects.filter(store_id=self.store.pk).exists())

    def test_deletion_closes_store_scoped_membership_and_pending_checkout(self):
        plan = SubscriptionPlan.objects.create(
            name="Store pack",
            slug="store-pack-delete-test",
            price="49.00",
            currency="MAD",
            duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
        )
        subscription = UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            selected_store=self.store,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
        )
        payment = SubscriptionPayment.objects.create(
            user=self.user,
            plan=plan,
            selected_store=self.store,
            plan_name=plan.name,
            amount=plan.price,
            currency=plan.currency,
            duration_days=plan.duration_days,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
        )

        response = self.delete()

        self.assertEqual(response.status_code, 204, response.content)
        subscription.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(subscription.status, UserSubscription.Status.CANCELED)
        self.assertIsNone(subscription.selected_store_id)
        self.assertEqual(payment.status, SubscriptionPayment.Status.CANCELED)
        self.assertIsNone(payment.selected_store_id)

    def test_confirmed_owner_can_delete_store_with_catalog_data(self):
        section = CatalogSection.objects.create(
            store=self.store,
            key="products",
            label="Products",
        )
        product = Product.objects.create(
            store=self.store,
            section=section,
            name="Test product",
            ref="TEST-1",
        )
        variant = StockVariant.objects.create(
            store=self.store,
            product=product,
            quantity=3,
        )
        option = ProductOption.objects.create(
            store=self.store, product=product, code="color", label="Color",
            kind=ProductOption.Kind.COLOR, position=0,
        )
        value = ProductOptionValue.objects.create(
            option=option, code="blue", label="Blue", position=0,
        )
        selection = VariantSelection.objects.create(
            variant=variant, option=option, value=value,
        )

        response = self.delete()

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(Store.objects.filter(pk=self.store.pk).exists())
        self.assertFalse(CatalogSection.objects.filter(pk=section.pk).exists())
        self.assertFalse(Product.objects.filter(pk=product.pk).exists())
        self.assertFalse(StockVariant.objects.filter(pk=variant.pk).exists())
        self.assertFalse(VariantSelection.objects.filter(pk=selection.pk).exists())


class StorefrontScaffoldStateTests(TestCase):
    def setUp(self):
        self.user = AuthUser.objects.create_user(email="deploy@test.local", password="secret12345")
        _set_jwt_cookie(self.client, self.user)
        self.store = Store.objects.create(slug="deploy-store", name="Deploy Store")
        StoreMember.objects.create(user=self.user, store=self.store, role=StoreMember.Roles.OWNER)

    @override_settings(STOREFRONT_DEPLOYMENT_MODE="local")
    @patch("api.views.enqueue_storefront_deployment")
    def test_local_preview_can_be_restarted_without_provider_work(self, enqueue):
        req = StorefrontRequest.objects.create(
            store=self.store, design="basic", name=self.store.name, slug=self.store.slug,
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            pipeline_state=StorefrontRequest.PIPELINE_LIVE,
            deployed_url="http://127.0.0.1:5190",
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/api/requests/{req.id}/scaffold")
        self.assertEqual(response.status_code, 202, response.content)
        enqueue.assert_called_once_with(req.id, force=True, oidc_token="")

    @patch("api.views.enqueue_storefront_deployment")
    def test_pending_request_can_start_scaffolding(self, enqueue):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_PENDING,
        )
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f"/api/requests/{req.id}/scaffold")

        self.assertEqual(resp.status_code, 202, resp.content)
        req.refresh_from_db()
        self.assertEqual(req.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(req.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)
        self.assertEqual(req.log, "")
        self.assertEqual(req.deploy_log, "")
        self.assertEqual(req.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        enqueue.assert_called_once_with(req.id, force=True, oidc_token="")

    @patch("api.views.refresh_deployment_status")
    def test_polling_request_refreshes_provider_readiness(self, refresh_status):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            vercel_deployment_id="dpl_poll_test",
        )

        def mark_ready(deployment):
            deployment.status = StorefrontRequest.STATUS_CREATED
            deployment.deploy_status = StorefrontRequest.DEPLOY_DEPLOYED
            return deployment

        refresh_status.side_effect = mark_ready

        response = self.client.get(f"/api/requests/{req.id}/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], StorefrontRequest.STATUS_CREATED)
        self.assertEqual(response.json()["deploy_status"], StorefrontRequest.DEPLOY_DEPLOYED)
        refresh_status.assert_called_once()

    def test_event_stream_returns_safe_versioned_state_and_resumes(self):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            internal_error="must never reach merchant",
            log="internal provider log",
        )
        ensure_initial_deployment_event(req.pk)
        record_deployment_state(
            req.pk,
            StorefrontRequest.PIPELINE_BUILDING,
            message="deployment.building",
        )

        response = self.client.get(
            f"/api/requests/{req.id}/events?once=1",
            HTTP_LAST_EVENT_ID="1",
            HTTP_ACCEPT="text/event-stream",
        )
        body = b"".join(response.streaming_content).decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertIn("id: 2", body)
        self.assertNotIn("id: 1\n", body)
        self.assertIn('"state":"building"', body)
        self.assertNotIn("must never reach merchant", body)
        self.assertNotIn("internal provider log", body)

    @patch("api.views.refresh_deployment_status")
    def test_fresh_event_stream_reconciles_and_does_not_replay_old_failure(self, refresh_status):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            vercel_deployment_id="dpl_retry_test",
        )
        ensure_initial_deployment_event(req.pk)
        record_deployment_state(
            req.pk,
            StorefrontRequest.PIPELINE_FAILED,
            message="deployment.failed",
        )
        current = record_deployment_state(
            req.pk,
            StorefrontRequest.PIPELINE_ACCEPTED,
            message="deployment.accepted",
            reset=True,
        )

        response = self.client.get(
            f"/api/requests/{req.id}/events?once=1",
            HTTP_ACCEPT="text/event-stream",
        )
        body = b"".join(response.streaming_content).decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"id: {current.event_version}", body)
        self.assertIn('"state":"accepted"', body)
        self.assertNotIn('"state":"failed"', body)
        refresh_status.assert_called_once()

    @patch("api.views.enqueue_storefront_deployment")
    def test_failed_request_can_retry_scaffolding(self, enqueue):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_FAILED,
            deploy_status=StorefrontRequest.DEPLOY_FAILED,
            log="Previous attempt failed",
            deploy_log="Previous attempt failed",
        )

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f"/api/requests/{req.id}/scaffold")

        self.assertEqual(resp.status_code, 202, resp.content)
        req.refresh_from_db()
        self.assertEqual(req.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(req.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)
        self.assertEqual(req.log, "")
        self.assertEqual(req.deploy_log, "")
        self.assertEqual(req.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        enqueue.assert_called_once_with(req.id, force=True, oidc_token="")

    @patch("api.views.enqueue_storefront_deployment")
    def test_stalled_accepted_request_can_retry_scaffolding(self, enqueue):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            task_id="cf2d97e5-7e68-4e86-92bd-eb84bdb7e25b",
            last_heartbeat_at=timezone.now() - timedelta(minutes=7),
        )

        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(f"/api/requests/{req.id}/scaffold")

        self.assertEqual(resp.status_code, 202, resp.content)
        enqueue.assert_called_once_with(req.id, force=True, oidc_token="")

    def test_created_request_cannot_be_scaffolded_again(self):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_CREATED,
        )
        resp = self.client.post(f"/api/requests/{req.id}/scaffold")
        self.assertEqual(resp.status_code, 409)

    def test_redesign_reuses_delivery_record_and_preserves_infrastructure_history(self):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
            pipeline_state=StorefrontRequest.PIPELINE_LIVE,
            vercel_project_id="prj_keep",
            cloudflare_dns_record_id="dns_keep",
            vercel_deployment_id="dpl_old",
            github_commit_sha="a" * 40,
            target_revision="a" * 40,
            deployed_revision="a" * 40,
            last_known_good_revision="a" * 40,
            last_known_good_deployment_id="dpl_old",
            deployed_url="https://deploy-store.alfirdaousstore.com",
        )
        ensure_initial_deployment_event(req.pk)
        old_event_count = req.deployment_events.count()

        response = self.client.post(
            "/api/requests/",
            {
                "store": self.store.id,
                "design": "basic",
                "config": {"store_name": "New Name", "brand_color": "#123456"},
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201, response.content)
        req.refresh_from_db()
        self.assertEqual(response.json()["id"], req.id)
        self.assertEqual(req.design, "basic")
        self.assertEqual(req.status, StorefrontRequest.STATUS_PENDING)
        self.assertEqual(req.vercel_project_id, "prj_keep")
        self.assertEqual(req.cloudflare_dns_record_id, "dns_keep")
        self.assertEqual(req.vercel_deployment_id, "")
        self.assertEqual(req.deployed_url, "https://deploy-store.alfirdaousstore.com")
        self.assertEqual(req.last_known_good_revision, "a" * 40)
        self.assertEqual(req.last_known_good_deployment_id, "dpl_old")
        self.assertEqual(req.operation, StorefrontRequest.OPERATION_REDESIGN)
        self.assertEqual(req.deployment_events.count(), old_event_count)
        self.store.config.refresh_from_db()
        self.assertEqual(self.store.config.design, "basic")

        record_deployment_state(req.pk, StorefrontRequest.PIPELINE_LIVE, message="deployment.live")
        self.store.refresh_from_db()
        self.store.config.refresh_from_db()
        self.assertEqual(self.store.name, "New Name")
        self.assertEqual(self.store.config.design, "basic")
        self.assertEqual(self.store.config.brand_color, "#123456")

    def test_redesign_preserves_omitted_contact_and_accepts_explicit_blank(self):
        req = StorefrontRequest.objects.create(
            store=self.store, design="basic", name=self.store.name, slug=self.store.slug,
        )
        for saved, supplied, expected in [
            ("", None, ""),
            ("support@example.test", None, "support@example.test"),
            ("support@example.test", "", ""),
        ]:
            with self.subTest(saved=saved, supplied=supplied):
                config = self.store.config
                config.contact_email = saved
                config.save(update_fields=["contact_email"])
                req.status = StorefrontRequest.STATUS_CREATED
                req.deploy_status = StorefrontRequest.DEPLOY_DEPLOYED
                req.pipeline_state = StorefrontRequest.PIPELINE_LIVE
                req.save(update_fields=["status", "deploy_status", "pipeline_state"])
                payload = {"store_name": self.store.name}
                if supplied is not None:
                    payload["contact_email"] = supplied
                response = self.client.post(
                    "/api/requests/",
                    {"store": self.store.id, "design": "basic", "config": payload},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 201, response.content)
                req.refresh_from_db()
                self.assertEqual(req.contact_email, expected)
                config.refresh_from_db()
                self.assertEqual(config.contact_email, saved)
                record_deployment_state(req.pk, StorefrontRequest.PIPELINE_LIVE)
                config.refresh_from_db()
                self.assertEqual(config.contact_email, expected)

    def test_retry_clears_values_from_the_previous_build(self):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_FAILED,
            deploy_status=StorefrontRequest.DEPLOY_FAILED,
            pipeline_state=StorefrontRequest.PIPELINE_FAILED,
            vercel_project_id="prj_keep",
            cloudflare_dns_record_id="dns_keep",
            vercel_deployment_id="dpl_stale",
            deployed_url="https://stale.example",
            github_commit_sha="abc123",
            attempt_count=2,
        )
        with patch("api.views.enqueue_storefront_deployment"):
            response = self.client.post(f"/api/requests/{req.id}/scaffold")

        self.assertEqual(response.status_code, 202, response.content)
        req.refresh_from_db()
        self.assertEqual(req.vercel_project_id, "prj_keep")
        self.assertEqual(req.cloudflare_dns_record_id, "dns_keep")
        self.assertEqual(req.vercel_deployment_id, "")
        self.assertEqual(req.deployed_url, "")
        self.assertEqual(req.github_commit_sha, "")
        self.assertEqual(req.attempt_count, 0)

    def test_free_plan_rejects_premium_deploy_request(self):
        resp = self.client.post(
            "/api/requests/",
            {
                "store": self.store.id,
                "design": "beauty",
                "config": {"store_name": self.store.name},
            },
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Premium plan", str(resp.json()))

    @patch("api.views.enqueue_storefront_deployment")
    def test_free_plan_blocks_legacy_premium_request_at_scaffold_time(self, enqueue):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="story",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_PENDING,
        )

        resp = self.client.post(f"/api/requests/{req.id}/scaffold")

        self.assertEqual(resp.status_code, 403, resp.content)
        enqueue.assert_not_called()

    def test_premium_plan_accepts_premium_deploy_request(self):
        plan = SubscriptionPlan.objects.get(slug="premium")
        UserSubscription.objects.create(
            user=self.user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"designs.premium": True},
        )
        resp = self.client.post(
            "/api/requests/",
            {
                "store": self.store.id,
                "design": "beauty",
                "config": {"store_name": self.store.name},
            },
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["design"], "beauty")


class StorefrontEventStreamASGITests(TestCase):
    def setUp(self):
        self.user = AuthUser.objects.create_user(email="asgi-stream@test.local", password="secret12345")
        self.store = Store.objects.create(slug="asgi-stream-store", name="ASGI Stream Store")
        StoreMember.objects.create(user=self.user, store=self.store, role=StoreMember.Roles.OWNER)

    @patch("api.views._reconcile_deployment_truth")
    @patch("api.views._deployment_events_after")
    def test_event_stream_uses_working_async_iterator_under_asgi(self, read_events, reconcile):
        req = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
        )
        read_events.return_value = [SimpleNamespace(
            version=1,
            state=StorefrontRequest.PIPELINE_ACCEPTED,
            progress=0,
            message="deployment.accepted",
            snapshot={"id": req.pk, "pipeline_state": StorefrontRequest.PIPELINE_ACCEPTED},
        )]
        request = AsyncRequestFactory().get(
            f"/api/requests/{req.id}/events",
            headers={"accept": "text/event-stream"},
        )
        force_authenticate(request, user=self.user)

        response = RequestEventsView.as_view()(request, pk=req.id)

        async def consume_first_chunk(iterator):
            try:
                return await anext(iterator)
            finally:
                await iterator.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_async)
        first_chunk = asyncio.run(consume_first_chunk(response.streaming_content))
        self.assertIn(b"event: deployment.updated", first_chunk)
