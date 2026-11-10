from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from .cloud import deploy_storefront_cloud, validate_pipeline_configuration
from .domains import default_storefront_url
from .services import cleanup_storefront_infrastructure
from .vercel_deploy import (
    DeployError,
    DeploymentResult,
    _ensure_platform_domain_verification,
    _resolve_domain,
    _verify_provider_domain,
)


@override_settings(STOREFRONT_USE_VERCEL_DOMAIN=True, STOREFRONT_BASE_DOMAIN="example.com",
                   VERCEL_API_TOKEN="hosting", CLOUDFLARE_API_TOKEN="", GITHUB_TOKEN="source",
                   DESIGN_STOCK_REPO="owner/source", STOREFRONT_SOURCE_TOKEN="",
                   STOREFRONT_DEPLOYMENT_REPO="", STOREFRONT_DEPLOYMENT_BRANCH="main",
                   STOREFRONT_API_URL="https://api.example.com", STOREFRONT_RELEASE_REVISION="a" * 40)
class ProviderDomainTests(SimpleTestCase):
    @override_settings(
        STOREFRONT_BASE_DOMAIN="tijarago.com",
        CLOUDFLARE_API_TOKEN="cloudflare",
        CLOUDFLARE_ZONE_ID="zone",
    )
    @patch("storefront_factory.vercel_deploy.requests.post")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_platform_domain_verification_creates_txt_and_verifies(self, get, post):
        get.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"success": True, "result": []}),
        )
        post.side_effect = [
            Mock(status_code=200, json=Mock(return_value={"success": True, "result": {"id": "txt"}})),
            Mock(status_code=200, json=Mock(return_value={"verified": True})),
        ]
        _ensure_platform_domain_verification(
            "demo-basic.tijarago.com",
            "prj_basic",
            {
                "verified": False,
                "verification": [{
                    "type": "TXT",
                    "domain": "_vercel.tijarago.com",
                    "value": "vc-domain-verify=demo-basic.tijarago.com,proof",
                }],
            },
            Mock(),
        )
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].kwargs["json"]["content"].endswith(",proof"))

    def test_default_address_is_provider_owned_and_explicit_domain_survives(self):
        self.assertEqual(default_storefront_url("canary"), "https://canary.vercel.app")
        req = SimpleNamespace(slug="canary", domain="https://merchant.example")
        self.assertEqual(_resolve_domain(req, Mock()), "merchant.example")
        self.assertEqual(req.domain, "https://merchant.example")

    def test_provider_preflight_does_not_require_cloudflare_but_custom_domain_does(self):
        validate_pipeline_configuration()
        req = SimpleNamespace(slug="canary", domain="https://merchant.example")
        with self.assertRaisesMessage(DeployError, "CLOUDFLARE_API_TOKEN"):
            validate_pipeline_configuration(req)

    @patch("storefront_factory.vercel_deploy._vercel_read")
    def test_unowned_provider_address_is_rejected(self, read):
        read.return_value = Mock(status_code=404)
        with self.assertRaisesMessage(DeployError, "not assigned"):
            _verify_provider_domain("canary.vercel.app", "prj_canary", Mock())

    @override_settings(STOREFRONT_USE_VERCEL_DOMAIN=False)
    @patch("storefront_factory.cloud._trigger_deployment")
    @patch("storefront_factory.cloud._set_env_vars")
    @patch("storefront_factory.cloud._verify_provider_domain")
    @patch("storefront_factory.cloud._attach_domain")
    @patch("storefront_factory.cloud._ensure_dns_record")
    @patch("storefront_factory.cloud._get_project")
    def test_recorded_provider_domain_survives_default_change_without_dns(self, project, dns, attach, verify, env, trigger):
        req = SimpleNamespace(slug="canary", domain="https://canary.vercel.app", save=Mock())
        project.return_value = {"id":"prj_canary", "rootDirectory":"apps/canary",
                                "link":{"repo":"source", "org":"owner"}}
        trigger.return_value = DeploymentResult("https://protected-deployment.vercel.app", "dpl_canary", "READY")
        deploy_storefront_cloud(req, Mock())
        verify.assert_called_once()
        dns.assert_not_called()
        attach.assert_not_called()
        self.assertEqual(req.deployed_url, "https://canary.vercel.app")

    @override_settings(STOREFRONT_USE_VERCEL_DOMAIN=False)
    @patch("storefront_factory.services.remove_github_storefront_app", return_value=True)
    @patch("storefront_factory.services.remove_vercel_project", return_value=True)
    @patch("storefront_factory.services.remove_cloudflare_dns_record")
    def test_recorded_provider_cleanup_needs_no_cloudflare(self, dns, project, github):
        req = SimpleNamespace(slug="canary", domain="https://canary.vercel.app", vercel_project_id="prj_canary",
                              deployed_url="https://canary.vercel.app", deploy_status="deployed")
        self.assertEqual(cleanup_storefront_infrastructure([req]), {"dns_records":0,"vercel_projects":1,"github_apps":1})
        dns.assert_not_called()


class RecordedPublicDomainTests(TestCase):
    @override_settings(STOREFRONT_USE_VERCEL_DOMAIN=False)
    def test_runtime_config_keeps_recorded_provider_address_after_default_change(self):
        from api.storefront_config import _storefront_domain
        from django.utils import timezone
        from store.models import Store, StoreConfig, StorefrontDomain
        from .models import StorefrontRequest
        store = Store.objects.create(slug="provider-canary", name="Provider canary")
        config, _ = StoreConfig.objects.get_or_create(store=store)
        StorefrontRequest.objects.create(store=store, slug=store.slug, name=store.name, design="basic",
            deployed_url="https://provider-canary.vercel.app", deployed_revision="a" * 40)
        self.assertEqual(_storefront_domain(store, config), "https://provider-canary.vercel.app")
        from accounts.customer_email_verification import customer_storefront_url
        self.assertEqual(customer_storefront_url(store), "https://provider-canary.vercel.app")
        custom = StorefrontDomain.objects.create(
            store=store, hostname="merchant.example.com", kind="custom", status="active",
            provider_verified=True, certificate_status="valid", health_verified_at=timezone.now(),
            is_primary=True,
        )
        store.refresh_from_db()
        self.assertEqual(customer_storefront_url(store), f"https://{custom.hostname}")
