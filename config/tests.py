from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .middleware import PlatformCsrfViewMiddleware
from .origins import frontend_url_for_origin, is_local_development_origin


class StorefrontOriginConfigurationTests(TestCase):
    def test_broad_vercel_origins_are_not_allowed_in_production(self):
        patterns = "\n".join(settings.CORS_ALLOWED_ORIGIN_REGEXES)
        self.assertNotIn("vercel", patterns)

    def test_generated_storefront_subdomain_is_a_trusted_csrf_origin(self):
        request = RequestFactory().post(
            "/api/orders/",
            HTTP_ORIGIN=f"https://look.{settings.STOREFRONT_BASE_DOMAIN}",
            secure=True,
        )
        middleware = PlatformCsrfViewMiddleware(lambda _request: HttpResponse())

        self.assertIn(f"https://*.{settings.STOREFRONT_BASE_DOMAIN}", settings.CSRF_TRUSTED_ORIGINS)
        self.assertTrue(middleware._origin_verified(request))

    def test_unrelated_domain_is_not_a_trusted_csrf_origin(self):
        request = RequestFactory().post(
            "/api/orders/",
            HTTP_ORIGIN="https://look.example.net",
            secure=True,
        )
        middleware = PlatformCsrfViewMiddleware(lambda _request: HttpResponse())

        self.assertFalse(middleware._origin_verified(request))

    @override_settings(DEBUG=True)
    def test_private_lan_origin_is_dynamic_in_development(self):
        request = RequestFactory().post(
            "/accounts/register",
            HTTP_ORIGIN="https://10.189.110.138:5178",
            secure=True,
        )
        middleware = PlatformCsrfViewMiddleware(lambda _request: HttpResponse())

        self.assertTrue(middleware._origin_verified(request))
        self.assertTrue(is_local_development_origin("https://192.168.50.7:5180"))
        self.assertEqual(
            frontend_url_for_origin(
                request.META["HTTP_ORIGIN"],
                app="dashboard",
                fallback="https://dash.example.test",
                allow_local=True,
            ),
            "https://10.189.110.138:5179",
        )

    @override_settings(DEBUG=True)
    def test_public_ip_and_unrelated_hostname_are_not_dynamic_origins(self):
        self.assertFalse(is_local_development_origin("https://8.8.8.8:5178"))
        self.assertFalse(is_local_development_origin("https://attacker.example:5178"))
