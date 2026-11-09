from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from config.origins import active_custom_origin_store_id, custom_origin_matches_request
from storefront_factory.domains import public_storefront_url

from .domain_services import (
    DomainConnectionError, _provider_records, create_custom_domain,
    disconnect_domain, domain_redirect_target, make_primary,
    set_domain_redirect, verify_domain,
)
from .domain_validation import normalize_storefront_hostname
from .models import Store, StoreMember, StorefrontDomain


@override_settings(STOREFRONT_BASE_DOMAIN="tijarago.test")
class DomainValidationTests(TestCase):
    def test_normalizes_case_trailing_dot_and_idna(self):
        hostname, display = normalize_storefront_hostname("BÜCHER.DE.")
        self.assertEqual(hostname, "xn--bcher-kva.de")
        self.assertEqual(display, "BÜCHER.DE")

    def test_rejects_urls_ports_ips_reserved_and_platform_hosts(self):
        for value in ("https://example.com", "example.com:443", "127.0.0.1",
                      "shop.local", "tijarago.test", "shop.tijarago.test"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_storefront_hostname(value)


@override_settings(STOREFRONT_BASE_DOMAIN="tijarago.test", STOREFRONT_CUSTOM_DOMAINS_ENABLED=True)
class StorefrontDomainLifecycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.store = Store.objects.create(name="Domain Store", slug="domain-store")
        self.owner = get_user_model().objects.create_user(email="owner@domain.test", password="password123")
        MerchantProfile.objects.create(user=self.owner)
        StoreMember.objects.create(store=self.store, user=self.owner, role=StoreMember.Roles.OWNER)

    def test_generic_config_write_cannot_publish_arbitrary_domain(self):
        client = APIClient(); client.force_authenticate(self.owner)
        response = client.patch(reverse("store-config", args=[self.store.pk]),
                                {"domain": "https://attacker.example"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.store.config.refresh_from_db()
        self.assertEqual(self.store.config.domain, "")

    def test_claim_is_idempotent_for_store_and_unique_globally(self):
        first, created = create_custom_domain(self.store, "shop.example.com")
        again, repeated = create_custom_domain(self.store, "SHOP.EXAMPLE.COM.")
        self.assertTrue(created); self.assertFalse(repeated); self.assertEqual(first.pk, again.pk)
        other = Store.objects.create(name="Other", slug="other")
        with self.assertRaises(DomainConnectionError):
            create_custom_domain(other, "shop.example.com")

    def test_companion_domain_records_canonical_redirect(self):
        canonical, _ = create_custom_domain(self.store, "www.example.com")
        companion, _ = create_custom_domain(self.store, "example.com")

        set_domain_redirect(companion, canonical.hostname)

        self.assertEqual(domain_redirect_target(companion), "www.example.com")

    @patch("subscriptions.policies.enforce_operation")
    def test_include_www_always_makes_www_canonical_and_apex_redirect(self, enforce):
        client = APIClient(); client.force_authenticate(self.owner)

        response = client.post(
            reverse("storefront-domain-list", args=[self.store.pk]),
            {"hostname": "example.com", "include_www": True}, format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual([item["hostname"] for item in response.data["domains"]], [
            "www.example.com", "example.com",
        ])
        self.assertEqual(response.data["domains"][1]["redirect_to"], "www.example.com")

    def test_only_healthy_domain_can_be_primary_and_fallback_cannot_disconnect(self):
        pending = StorefrontDomain.objects.create(
            store=self.store, hostname="shop.example.com", kind="custom", status="awaiting_dns",
        )
        with self.assertRaises(DomainConnectionError):
            make_primary(pending.pk)
        fallback = self.store.storefront_domains.get(kind="platform")
        with self.assertRaises(DomainConnectionError):
            disconnect_domain(fallback.pk)

    @patch("store.domain_jobs.enqueue_domain_disconnection")
    def test_pending_domain_delete_is_immediate_and_does_not_require_queue(self, enqueue):
        domain, _ = create_custom_domain(self.store, "typo.example.com")
        client = APIClient(); client.force_authenticate(self.owner)

        response = client.delete(reverse("storefront-domain-detail", args=[self.store.pk, domain.pk]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(StorefrontDomain.objects.filter(pk=domain.pk).exists())
        enqueue.assert_not_called()

    @patch("store.domain_jobs.enqueue_domain_disconnection")
    def test_stuck_unattached_removal_can_be_deleted_on_retry(self, enqueue):
        domain, _ = create_custom_domain(self.store, "stuck.example.com")
        domain.status = StorefrontDomain.Status.REMOVING
        domain.save(update_fields=["status", "updated_at"])
        client = APIClient(); client.force_authenticate(self.owner)

        response = client.delete(reverse("storefront-domain-detail", args=[self.store.pk, domain.pk]))

        self.assertEqual(response.status_code, 204)
        self.assertFalse(StorefrontDomain.objects.filter(pk=domain.pk).exists())
        enqueue.assert_not_called()

    @patch("store.domain_services.verify_domain")
    def test_verify_endpoint_returns_completed_state_without_queue(self, verify):
        domain, _ = create_custom_domain(self.store, "verify.example.com")
        domain.status = StorefrontDomain.Status.AWAITING_DNS
        verify.return_value = domain
        client = APIClient(); client.force_authenticate(self.owner)

        response = client.post(reverse("storefront-domain-verify", args=[self.store.pk, domain.pk]))

        self.assertEqual(response.status_code, 200)
        verify.assert_called_once_with(domain.pk)

    def test_public_url_ignores_unverified_legacy_and_pending_domains(self):
        self.store.config.domain = "https://unverified.example"
        self.store.config.save(update_fields=["domain"])
        StorefrontDomain.objects.create(store=self.store, hostname="pending.example", kind="custom", status="awaiting_dns")
        self.assertEqual(public_storefront_url(self.store), "https://domain-store.tijarago.test")

    def test_exact_active_origin_is_scoped_to_storefront_paths_and_tenant(self):
        domain = StorefrontDomain.objects.create(
            store=self.store, hostname="shop.example.com", kind="custom", status="active",
            provider_verified=True, certificate_status="valid", health_verified_at=timezone.now(),
        )
        cache.clear()
        self.assertEqual(active_custom_origin_store_id("https://shop.example.com", "/api/storefront/config/"), self.store.pk)
        self.assertIsNone(active_custom_origin_store_id("https://shop.example.com", "/api/store/1/config/"))
        self.assertIsNone(active_custom_origin_store_id("http://shop.example.com", "/api/storefront/config/"))
        request = RequestFactory().get("/api/storefront/config/", HTTP_ORIGIN="https://shop.example.com",
                                       HTTP_X_STORE_SLUG=self.store.slug)
        self.assertTrue(custom_origin_matches_request(request))
        mismatch = RequestFactory().get("/api/storefront/config/", HTTP_ORIGIN="https://shop.example.com",
                                        HTTP_X_STORE_SLUG="other")
        self.assertFalse(custom_origin_matches_request(mismatch))
        domain.delete(); cache.clear()
        self.assertIsNone(active_custom_origin_store_id("https://shop.example.com", "/api/storefront/config/"))

    def test_non_owner_cannot_manage_domains(self):
        admin = get_user_model().objects.create_user(email="admin@domain.test", password="password123")
        StoreMember.objects.create(store=self.store, user=admin, role=StoreMember.Roles.ADMIN)
        client = APIClient(); client.force_authenticate(admin)
        response = client.get(reverse("storefront-domain-list", args=[self.store.pk]))
        self.assertEqual(response.status_code, 403)

    def _deployed_request(self):
        from storefront_factory.models import StorefrontRequest
        return StorefrontRequest.objects.create(
            store=self.store, slug=self.store.slug, name=self.store.name, design="basic",
            vercel_project_id="prj_test", deployed_revision="a" * 40,
        )

    def test_apex_dns_instructions_use_all_preferred_a_records(self):
        configuration = {
            "recommendedIPv4": [{"rank": 1, "value": ["216.198.79.1", "64.29.17.1"]}],
            "recommendedCNAME": [{"rank": 1, "value": "target.vercel-dns.com."}],
        }

        records = _provider_records(
            "tgoo.dpdns.org", configuration,
            {"apexName": "tgoo.dpdns.org", "verification": []},
        )

        self.assertEqual(records, [
            {"purpose": "routing", "type": "A", "name": "tgoo.dpdns.org", "value": "216.198.79.1"},
            {"purpose": "routing", "type": "A", "name": "tgoo.dpdns.org", "value": "64.29.17.1"},
        ])

    def test_subdomain_dns_instructions_prefer_one_cname(self):
        configuration = {
            "recommendedIPv4": [{"rank": 1, "value": ["216.198.79.1", "64.29.17.1"]}],
            "recommendedCNAME": [{"rank": 1, "value": "target.vercel-dns.com."}],
        }

        records = _provider_records(
            "shop.example.com", configuration,
            {"apexName": "example.com", "verification": []},
        )

        self.assertEqual(records, [
            {"purpose": "routing", "type": "CNAME", "name": "shop.example.com", "value": "target.vercel-dns.com."},
        ])

    @patch("store.domain_services.attach_external_domain")
    @patch("store.domain_services._doh_answers", return_value=[])
    def test_ownership_must_pass_before_provider_attachment(self, answers, attach):
        self._deployed_request()
        domain, _ = create_custom_domain(self.store, "shop.example.com")
        checked = verify_domain(domain.pk)
        self.assertEqual(checked.status, StorefrontDomain.Status.AWAITING_OWNERSHIP)
        self.assertEqual(checked.failure_code, "ownership_pending")
        attach.assert_not_called()

    @patch("store.domain_services._verify_health")
    @patch("store.domain_services.read_project_domain", return_value={"name": "shop.example.com", "verified": True})
    @patch("store.domain_services.read_domain_configuration", return_value={"recommendedCNAME": [
        {"rank": 1, "value": "target.vercel-dns.com."},
        {"rank": 2, "value": "fallback.vercel-dns.com."},
    ]})
    @patch("store.domain_services.attach_external_domain", return_value={"name": "shop.example.com", "verified": True})
    @patch("store.domain_services._doh_answers")
    def test_mocked_provider_flow_activates_only_after_all_checks(self, answers, attach, config, provider, health):
        self._deployed_request()
        domain, _ = create_custom_domain(self.store, "shop.example.com")
        ownership = f"tgo-domain-verification={domain.ownership_token}"
        answers.side_effect = lambda name, kind: (
            [ownership] if kind == "TXT" else ["target.vercel-dns.com"] if kind == "CNAME" else []
        )
        checked = verify_domain(domain.pk)
        self.assertEqual(checked.status, StorefrontDomain.Status.ACTIVE)
        self.assertTrue(checked.healthy)
        self.assertTrue(checked.ownership_verified_at)
        health.assert_called_once()

    @patch("store.domain_services._doh_answers")
    def test_rechecking_active_primary_domain_is_idempotent(self, answers):
        domain = StorefrontDomain.objects.create(
            store=self.store, hostname="shop.example.com", kind="custom", status="active",
            is_primary=True, provider_project_id="prj_test", provider_verified=True,
            certificate_status="valid", health_verified_at=timezone.now(),
        )

        checked = verify_domain(domain.pk)

        self.assertEqual(checked.status, StorefrontDomain.Status.ACTIVE)
        self.assertTrue(checked.is_primary)
        self.assertTrue(checked.healthy)
        answers.assert_not_called()

    @patch("store.domain_services._verify_health")
    @patch("store.domain_services.configure_external_domain_redirect")
    @patch("store.domain_services.read_project_domain", return_value={"name": "example.com", "verified": True})
    @patch("store.domain_services.read_domain_configuration", return_value={
        "recommendedIPv4": [{"rank": 1, "value": ["216.198.79.1"]}],
    })
    @patch("store.domain_services.attach_external_domain", return_value={
        "name": "example.com", "apexName": "example.com", "verified": True,
    })
    @patch("store.domain_services._doh_answers")
    def test_verified_companion_becomes_redirect_without_storefront_health_check(
        self, answers, attach, config, provider, configure_redirect, health,
    ):
        self._deployed_request()
        canonical = StorefrontDomain.objects.create(
            store=self.store, hostname="www.example.com", kind="custom", status="active",
            provider_project_id="prj_test", provider_verified=True,
            certificate_status="valid", health_verified_at=timezone.now(),
        )
        companion, _ = create_custom_domain(self.store, "example.com")
        set_domain_redirect(companion, canonical.hostname)
        ownership = f"tgo-domain-verification={companion.ownership_token}"
        answers.side_effect = lambda name, kind: (
            [ownership] if kind == "TXT" else ["216.198.79.1"] if kind == "A" else []
        )

        checked = verify_domain(companion.pk)

        self.assertEqual(checked.status, StorefrontDomain.Status.ACTIVE)
        configure_redirect.assert_called_once_with("example.com", "prj_test", "www.example.com")
        health.assert_not_called()

    @patch("store.domain_services.remove_external_domain")
    def test_disconnect_removes_only_exact_provider_alias_and_never_dns(self, remove):
        domain = StorefrontDomain.objects.create(
            store=self.store, hostname="shop.example.com", kind="custom", status="active",
            provider_project_id="prj_test", provider_verified=True,
            certificate_status="valid", health_verified_at=timezone.now(),
        )
        disconnect_domain(domain.pk)
        remove.assert_called_once_with("shop.example.com", "prj_test")
        self.assertFalse(StorefrontDomain.objects.filter(pk=domain.pk).exists())


@override_settings(STOREFRONT_CUSTOM_DOMAINS_ENABLED=False)
class DomainFeatureFlagTests(TestCase):
    def test_connect_is_disabled_by_default(self):
        store = Store.objects.create(name="Flag Store", slug="flag-store")
        owner = get_user_model().objects.create_user(email="owner@flag.test", password="password123")
        MerchantProfile.objects.create(user=owner)
        StoreMember.objects.create(store=store, user=owner, role=StoreMember.Roles.OWNER)
        client = APIClient(); client.force_authenticate(owner)
        response = client.post(reverse("storefront-domain-list", args=[store.pk]),
                               {"hostname": "shop.example.com"}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "feature_disabled")
