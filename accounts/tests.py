from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.cache import cache
from django.core import mail
from django.core import signing
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from accounts.models import MerchantProfile
from accounts.cookies import access_cookie_name, refresh_cookie_name
from accounts.email_verification import TOKEN_SALT, make_email_verification_token
from accounts.tokens import MERCHANT_SCOPE, PLATFORM_SCOPE, issue_scoped_tokens
from rest_framework_simplejwt.tokens import AccessToken
from subscriptions.models import UserSubscription


class CookieAuthenticationSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            email="merchant@example.com", password="pw", email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=self.user)
        self.client = APIClient(enforce_csrf_checks=True)
        access, _refresh = issue_scoped_tokens(self.user, MERCHANT_SCOPE)
        self.client.cookies[access_cookie_name("owner")] = access

    def test_authenticated_write_requires_csrf_token(self):
        response = self.client.post(reverse("store-list"), {"slug": "csrf-store", "name": "CSRF Store"}, format="json")
        self.assertEqual(response.status_code, 403)

    @override_settings(DEBUG=True)
    def test_csrf_endpoint_enables_authenticated_write(self):
        csrf = self.client.get("/accounts/csrf")
        self.assertEqual(csrf.status_code, 200)
        response = self.client.post(
            reverse("store-list"),
            {"slug": "csrf-store", "name": "CSRF Store"},
            format="json",
            HTTP_X_CSRFTOKEN=csrf.data["csrfToken"],
            HTTP_ORIGIN="https://localhost:5179",
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_platform_token_cannot_be_reused_as_merchant_session(self):
        platform_access, _refresh = issue_scoped_tokens(self.user, PLATFORM_SCOPE)
        self.client.cookies[access_cookie_name("owner")] = platform_access
        csrf = self.client.get("/accounts/csrf").data["csrfToken"]

        response = self.client.get(reverse("store-list"), HTTP_X_CSRFTOKEN=csrf)

        self.assertEqual(response.status_code, 401)

    def test_customer_only_account_cannot_request_merchant_scope(self):
        customer = get_user_model().objects.create_user(email="customer@example.com", password="password123")

        response = APIClient().post(
            "/accounts/token",
            {"email": customer.email, "password": "password123", "scope": "owner"},
            format="json",
        )

        self.assertEqual(response.status_code, 401)
        invalid_password = APIClient().post(
            "/accounts/token",
            {"email": customer.email, "password": "incorrect", "scope": "owner"},
            format="json",
        )
        self.assertEqual(response.data, invalid_password.data)

    def test_merchant_registration_creates_explicit_principal(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = APIClient().post(
                "/accounts/register",
                {
                    "email": "new-merchant@example.com",
                    "confirm_email": "new-merchant@example.com",
                    "password": "secure-pass-123",
                    "confirm_password": "secure-pass-123",
                    "first_name": "New",
                    "last_name": "Merchant",
                    "business_name": "New Market",
                    "phone_number": "+212 612-345-678",
                    "preferred_language": "en",
                    "terms_accepted": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        profile = MerchantProfile.objects.select_related("user").get(user__email="new-merchant@example.com")
        self.assertEqual(profile.business_name, "New Market")
        self.assertEqual(str(profile.phone_number), "+212612345678")
        self.assertEqual(profile.preferred_language, "en")
        self.assertFalse(profile.is_phone_verified)
        self.assertIsNotNone(profile.terms_accepted_at)
        self.assertFalse(profile.user.is_active)
        self.assertIsNone(profile.user.email_verified_at)
        self.assertTrue(response.data["verification_required"])
        self.assertEqual(mail.outbox[-1].to, ["new-merchant@example.com"])
        self.assertEqual(mail.outbox[-1].extra_headers["X-TijaraGO-Event"], "merchant.email_verification")
        self.assertIn("Confirm my email:", mail.outbox[-1].body)
        self.assertIn("/verify-email?token=", mail.outbox[-1].body)
        self.assertNotIn("/accounts/verify-email?token=", mail.outbox[-1].body)
        verification_html = mail.outbox[-1].alternatives[0][0]
        self.assertIn(settings.SHOWROOM_EMAIL_BRAND_COLOR, verification_html)
        self.assertIn(settings.SHOWROOM_EMAIL_LOGO_URL, verification_html)

    def test_merchant_registration_does_not_require_business_name(self):
        payload = self.merchant_registration_payload(
            email="merchant-without-business@example.com",
        )
        payload.pop("business_name")

        with self.captureOnCommitCallbacks(execute=True):
            response = APIClient().post("/accounts/register", payload, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        profile = MerchantProfile.objects.get(
            user__email="merchant-without-business@example.com",
        )
        self.assertEqual(profile.business_name, "")
        self.assertIn("Votre compte marchand a été créé", mail.outbox[-1].body)
        self.assertNotIn("Activité:", mail.outbox[-1].body)

    def merchant_registration_payload(self, **overrides):
        payload = {
            "email": "verify-me@example.com",
            "confirm_email": "verify-me@example.com",
            "password": "secure-pass-123",
            "confirm_password": "secure-pass-123",
            "first_name": "Nadia",
            "last_name": "Amrani",
            "business_name": "Atelier Nadia",
            "phone_number": "+212612345678",
            "preferred_language": "fr",
            "terms_accepted": True,
        }
        if "email" in overrides and "confirm_email" not in overrides:
            payload["confirm_email"] = overrides["email"]
        payload.update(overrides)
        return payload

    @override_settings(
        DEBUG=True,
        EMAIL_VERIFICATION_FRONTEND_URL="https://old-address.example/verify-email",
        MERCHANT_DASHBOARD_URL="https://dash.example.test",
    )
    def test_local_registration_and_ready_email_preserve_current_browser_host(self):
        local_showroom = "https://10.189.110.138:5178"
        local_dashboard = "https://10.189.110.138:5179"
        with self.captureOnCommitCallbacks(execute=True):
            registered = APIClient().post(
                "/accounts/register",
                self.merchant_registration_payload(
                    email="dynamic-origin@example.com",
                    preferred_language="en",
                ),
                format="json",
                HTTP_ORIGIN=local_showroom,
            )

        self.assertEqual(registered.status_code, 201, registered.data)
        self.assertIn(f"{local_dashboard}/verify-email?token=", mail.outbox[-1].body)

        user = get_user_model().objects.get(email="dynamic-origin@example.com")
        token = make_email_verification_token(user)
        with self.captureOnCommitCallbacks(execute=True):
            verified = APIClient().post(
                "/accounts/verify-email",
                {"token": token},
                format="json",
                HTTP_ORIGIN=local_dashboard,
            )

        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertEqual(mail.outbox[-1].extra_headers["X-TijaraGO-Event"], "merchant.space_ready")
        self.assertIn(f"Open my merchant space: {local_dashboard}", mail.outbox[-1].body)

    def test_merchant_registration_password_policy_matches_live_checklist(self):
        without_number = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(
                email="letters-only@example.com",
                password="letters-only-password",
                confirm_password="letters-only-password",
            ),
            format="json",
        )
        without_letter = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(
                email="numbers-only@example.com",
                password="123456789012",
                confirm_password="123456789012",
            ),
            format="json",
        )

        self.assertEqual(without_number.status_code, 400)
        self.assertEqual(without_letter.status_code, 400)
        self.assertFalse(get_user_model().objects.filter(email__in=[
            "letters-only@example.com", "numbers-only@example.com",
        ]).exists())

    def test_unverified_merchant_cannot_login(self):
        with self.captureOnCommitCallbacks(execute=True):
            registered = APIClient().post(
                "/accounts/register", self.merchant_registration_payload(), format="json",
            )
        self.assertEqual(registered.status_code, 201, registered.data)

        login = APIClient().post(
            "/accounts/token",
            {"email": "verify-me@example.com", "password": "secure-pass-123", "scope": "owner"},
            format="json",
        )
        self.assertEqual(login.status_code, 401)
        self.assertEqual(login.data, {"detail": "Unable to sign in with the provided information."})
        self.assertIn("Confirmer mon e-mail:", mail.outbox[-1].body)

    @override_settings(
        EMAIL_VERIFICATION_FRONTEND_URL="https://dash.example.test/verify-email",
        MERCHANT_DASHBOARD_URL="https://dash.example.test",
        MERCHANT_EMAIL_BRAND_COLOR="#2D6A4F",
        MERCHANT_EMAIL_BRAND_ACCENT_COLOR="#52B788",
        MERCHANT_EMAIL_BRAND_INK_COLOR="#1B4332",
        MERCHANT_EMAIL_LOGO_URL="https://dash.example.test/brand/merchant-green.png",
    )
    def test_email_verification_uses_frontend_and_activates_once_via_post(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/accounts/register", self.merchant_registration_payload(), format="json")
        user = get_user_model().objects.get(email="verify-me@example.com")
        token = make_email_verification_token(user)

        email_link = APIClient().get("/accounts/verify-email", {"token": token})
        self.assertEqual(email_link.status_code, 302)
        self.assertTrue(email_link.url.startswith("https://dash.example.test/verify-email?token="))
        user.refresh_from_db()
        self.assertFalse(user.is_active)

        with self.captureOnCommitCallbacks(execute=True):
            verified = APIClient().post("/accounts/verify-email", {"token": token}, format="json")
        self.assertEqual(verified.status_code, 200)
        self.assertTrue(verified.data["verified"])
        self.assertFalse(verified.data["already_verified"])
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)
        free_subscription = UserSubscription.objects.get(user=user, status=UserSubscription.Status.ACTIVE)
        self.assertEqual(free_subscription.plan.slug, "free")
        self.assertEqual(free_subscription.entitlement_scope, "all_owned_stores")
        self.assertIsNone(free_subscription.current_period_end)
        self.assertEqual(free_subscription.entitlements_snapshot, {})
        self.assertEqual(len(mail.outbox), 2)
        ready_email = mail.outbox[-1]
        self.assertEqual(ready_email.extra_headers["X-TijaraGO-Event"], "merchant.space_ready")
        self.assertIn("https://dash.example.test", ready_email.body)
        ready_html = ready_email.alternatives[0][0]
        self.assertIn("#2D6A4F", ready_html)
        self.assertIn("https://dash.example.test/brand/merchant-green.png", ready_html)

        verified_at = user.email_verified_at
        email_count = len(mail.outbox)
        replay = APIClient().post("/accounts/verify-email", {"token": token}, format="json")
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.data["already_verified"])
        self.assertEqual(len(mail.outbox), email_count)
        self.assertEqual(UserSubscription.objects.filter(user=user).count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.email_verified_at, verified_at)

        login = APIClient().post(
            "/accounts/token",
            {"email": user.email, "password": "secure-pass-123", "scope": "owner"},
            format="json",
        )
        self.assertEqual(login.status_code, 200, login.data)

        user.is_active = False
        user.save(update_fields=["is_active"])
        APIClient().post("/accounts/verify-email", {"token": token}, format="json")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_verification_lock_does_not_join_nullable_merchant_profile(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/accounts/register", self.merchant_registration_payload(), format="json")
        user = get_user_model().objects.get(email="verify-me@example.com")
        token = make_email_verification_token(user)

        with CaptureQueriesContext(connection) as queries:
            response = APIClient().post(
                "/accounts/verify-email", {"token": token}, format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        locked_user_query = next(
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "accounts_authuser"' in query["sql"]
        )
        self.assertNotIn(" JOIN ", locked_user_query.upper())

    def test_invalid_verification_token_has_neutral_api_error(self):
        response = APIClient().post(
            "/accounts/verify-email", {"token": "invalid"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data,
            {"detail": "This verification link is invalid or no longer available."},
        )

    @override_settings(SECRET_KEY="current-signing-key", SECRET_KEY_FALLBACKS=["previous-signing-key"])
    def test_verification_accepts_configured_previous_signing_key(self):
        with self.captureOnCommitCallbacks(execute=True):
            registered = APIClient().post(
                "/accounts/register",
                self.merchant_registration_payload(email="rotated-key@example.com"),
                format="json",
            )
        self.assertEqual(registered.status_code, 201, registered.data)
        user = get_user_model().objects.get(email="rotated-key@example.com")
        previous_key_token = signing.dumps(
            {"user_id": user.pk, "email": user.email},
            key="previous-signing-key",
            salt=TOKEN_SALT,
            compress=True,
        )

        verified = APIClient().post(
            "/accounts/verify-email", {"token": previous_key_token}, format="json",
        )

        self.assertEqual(verified.status_code, 200, verified.data)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)

    @override_settings(
        EMAIL_VERIFICATION_TIMEOUT_SECONDS=300,
    )
    def test_expired_verification_token_is_rejected(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/accounts/register", self.merchant_registration_payload(), format="json")
        user = get_user_model().objects.get(email="verify-me@example.com")
        issued_at = 1_800_000_000
        with patch("django.core.signing.time.time", return_value=issued_at):
            token = make_email_verification_token(user)
        with patch("django.core.signing.time.time", return_value=issued_at + 301):
            response = APIClient().post(
                "/accounts/verify-email", {"token": token}, format="json",
            )

        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    @override_settings(EMAIL_VERIFICATION_TIMEOUT_SECONDS=600)
    def test_verification_email_mentions_ten_minute_expiry(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = APIClient().post(
                "/accounts/register", self.merchant_registration_payload(), format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertIn("expire dans 10 minutes", mail.outbox[-1].body)

    def test_verification_resend_does_not_enumerate_accounts(self):
        with self.captureOnCommitCallbacks(execute=True):
            APIClient().post("/accounts/register", self.merchant_registration_payload(), format="json")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            immediate = APIClient().post(
                "/accounts/verify-email/resend", {"email": "verify-me@example.com"}, format="json",
            )
        self.assertEqual(immediate.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

        user = get_user_model().objects.get(email="verify-me@example.com")
        user.email_verification_sent_at = timezone.now() - timedelta(minutes=2)
        user.save(update_fields=["email_verification_sent_at"])
        with self.captureOnCommitCallbacks(execute=True):
            known = APIClient().post(
                "/accounts/verify-email/resend", {"email": "verify-me@example.com"}, format="json",
            )
        self.assertEqual(len(mail.outbox), 1)
        unknown = APIClient().post(
            "/accounts/verify-email/resend", {"email": "unknown@example.com"}, format="json",
        )
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.data, unknown.data)

    def test_registration_validates_confirmation_phone_terms_and_password(self):
        bad_phone = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(phone_number="0612345678"),
            format="json",
        )
        mismatch = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(confirm_password="different"),
            format="json",
        )
        email_mismatch = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(confirm_email="verify-mr@example.com"),
            format="json",
        )
        no_terms = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(terms_accepted=False),
            format="json",
        )
        weak_password = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(password="12345678", confirm_password="12345678"),
            format="json",
        )
        self.assertEqual(bad_phone.status_code, 400)
        self.assertIn("phone_number", bad_phone.data)
        self.assertIn("confirm_password", mismatch.data)
        self.assertIn("confirm_email", email_mismatch.data)
        self.assertIn("terms_accepted", no_terms.data)
        self.assertIn("non_field_errors", weak_password.data)
        self.assertFalse(get_user_model().objects.filter(email="verify-me@example.com").exists())

    def test_registration_normalizes_and_validates_multiple_country_numbers(self):
        french = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(
                email="france@example.com",
                phone_number="+33 6 12 34 56 78",
            ),
            format="json",
        )
        impossible = APIClient().post(
            "/accounts/register",
            self.merchant_registration_payload(
                email="impossible@example.com",
                phone_number="+212 12 34",
            ),
            format="json",
        )
        self.assertEqual(french.status_code, 201, french.data)
        profile = MerchantProfile.objects.get(user__email="france@example.com")
        self.assertEqual(str(profile.phone_number), "+33612345678")
        self.assertEqual(impossible.status_code, 400)
        self.assertIn("phone_number", impossible.data)

    def test_multi_role_login_requires_and_preserves_explicit_audience(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        client = APIClient()

        ambiguous = client.post(
            "/accounts/token",
            {"email": self.user.email, "password": "pw"},
            format="json",
        )
        self.assertEqual(ambiguous.status_code, 401)

        merchant = client.post(
            "/accounts/token",
            {"email": self.user.email, "password": "pw", "scope": "owner"},
            format="json",
        )
        self.assertEqual(merchant.status_code, 200)
        merchant_cookie = access_cookie_name("owner")
        self.assertIn(merchant_cookie, merchant.cookies)
        self.assertNotIn(access_cookie_name("staff"), merchant.cookies)
        self.assertEqual(
            AccessToken(merchant.cookies[merchant_cookie].value)["auth_scope"],
            MERCHANT_SCOPE,
        )
        morsel = merchant.cookies[merchant_cookie]
        self.assertTrue(morsel["secure"])
        self.assertTrue(morsel["httponly"])
        self.assertEqual(morsel["path"], "/")
        self.assertFalse(morsel["domain"])

    def test_refresh_rejects_ambiguous_multi_application_cookies(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        _access, merchant_refresh = issue_scoped_tokens(self.user, MERCHANT_SCOPE)
        _access, platform_refresh = issue_scoped_tokens(self.user, PLATFORM_SCOPE)
        client = APIClient()
        client.cookies[refresh_cookie_name("owner")] = merchant_refresh
        client.cookies[refresh_cookie_name("staff")] = platform_refresh

        ambiguous = client.post("/accounts/token/refresh", {}, format="json")
        self.assertEqual(ambiguous.status_code, 409)

        selected = client.post("/accounts/token/refresh", {"scope": "owner"}, format="json")
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.data["scope"], "owner")
        self.assertEqual(
            AccessToken(selected.cookies[access_cookie_name("owner")].value)["auth_scope"],
            MERCHANT_SCOPE,
        )

    def test_browser_cookie_names_are_neutral_and_host_bound(self):
        names = {
            access_cookie_name("staff"), refresh_cookie_name("staff"),
            access_cookie_name("owner"), refresh_cookie_name("owner"),
            access_cookie_name("client", "merchant-one"),
            refresh_cookie_name("client", "merchant-one"),
        }

        self.assertEqual(len(names), 6)
        for name in names:
            self.assertTrue(name.startswith("__Host-tgo-"))
            self.assertNotIn("staff", name)
            self.assertNotIn("owner", name)
            self.assertNotIn("client", name)
            self.assertNotIn("merchant-one", name)

    def test_legacy_cookie_is_accepted_and_refresh_writes_only_neutral_names(self):
        _access, merchant_refresh = issue_scoped_tokens(self.user, MERCHANT_SCOPE)
        client = APIClient()
        client.cookies["owner_refresh_token"] = merchant_refresh

        response = client.post(
            "/accounts/token/refresh", {"scope": "owner"}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(access_cookie_name("owner"), response.cookies)
        self.assertIn(refresh_cookie_name("owner"), response.cookies)
        self.assertNotIn("owner_access_token", response.cookies)


class UserManagerSecurityTests(TestCase):
    def test_create_superuser_cannot_disable_required_platform_flags(self):
        User = get_user_model()

        with self.assertRaisesMessage(ValueError, "is_staff=True"):
            User.objects.create_superuser(
                email="invalid-staff@example.com",
                password="password123",
                is_staff=False,
            )
        with self.assertRaisesMessage(ValueError, "is_superuser=True"):
            User.objects.create_superuser(
                email="invalid-superuser@example.com",
                password="password123",
                is_superuser=False,
            )
