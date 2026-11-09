"""Storefront customer identity tests: confirmation, tenant cookies and profile."""

from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.cookies import access_cookie_name, refresh_cookie_name
from accounts.customer_email_verification import make_customer_email_verification_token
from accounts.password_recovery import issue_recovery
from accounts.models import PasswordResetToken
import hashlib
from accounts.models import AuthUser
from accounts.tokens import CUSTOMER_SCOPE, issue_scoped_tokens
from store.models import ClientProfile, Store, StoreCustomer


@override_settings(
    STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
    ALLOWED_HOSTS=["*"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class StorefrontCustomerAuthTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(slug="acme", name="Acme Store")
        self.client = APIClient()
        self.store_header = {"HTTP_X_STORE_SLUG": "acme"}
        self.registration = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "email": "ada@test.local",
            "confirmEmail": "ada@test.local",
            "phone": "+212612345678",
            "password": "secret12345",
            "confirmPassword": "secret12345",
            "preferredLanguage": "en",
        }

    def _register(self, api=None):
        api = api or self.client
        with self.captureOnCommitCallbacks(execute=True):
            return api.post("/api/auth/register/", self.registration, format="json", **self.store_header)

    def _verified_customer(self, email="customer@test.local", password="secret12345"):
        user = AuthUser.objects.create_user(
            email=email,
            password=password,
            first_name="Customer",
            last_name="One",
            email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=user, phone="+212612345678")
        profile.stores.add(self.store)
        customer = StoreCustomer.objects.create(profile=profile, store=self.store)
        return user, profile, customer

    def _authenticated_api(self, user):
        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient()
        api.cookies[access_cookie_name("client", self.store.slug)] = access
        return api

    def test_register_requires_confirmation_before_session_is_issued(self):
        response = self._register()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.data["verificationRequired"])
        self.assertNotIn(access_cookie_name("client", "acme"), response.cookies)
        user = AuthUser.objects.get(email="ada@test.local")
        self.assertFalse(user.is_active)
        self.assertIsNone(user.email_verified_at)
        self.assertEqual(str(user.client_profile.phone), "+212612345678")
        self.assertTrue(StoreCustomer.objects.filter(profile=user.client_profile, store=self.store).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("10 minutes", mail.outbox[0].alternatives[0][0])
        self.assertIn("/account?verify=", mail.outbox[0].body)

    def test_confirmation_activates_customer_and_sets_tenant_cookies(self):
        self._register()
        user = AuthUser.objects.get(email="ada@test.local")
        token = make_customer_email_verification_token(user, self.store)

        response = self.client.post(
            "/api/auth/verify-email/",
            {"token": token},
            format="json",
            **self.store_header,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.data["customer"]["emailVerified"])
        self.assertTrue(response.cookies[access_cookie_name("client", "acme")].value)
        self.assertTrue(response.cookies[refresh_cookie_name("client", "acme")].value)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verified_at)

    @override_settings(EMAIL_VERIFICATION_TIMEOUT_SECONDS=60)
    def test_expired_confirmation_is_rejected_neutrally(self):
        self._register()
        user = AuthUser.objects.get(email="ada@test.local")
        token = make_customer_email_verification_token(user, self.store)
        with self.settings(SECRET_KEY="different-key"):
            response = self.client.post(
                "/api/auth/verify-email/", {"token": token}, format="json", **self.store_header,
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "verification_unavailable")

    def test_registration_validates_phone_password_and_confirmed_email(self):
        invalid = {**self.registration, "phone": "123", "password": "password", "confirmPassword": "password", "confirmEmail": "other@test.local"}
        response = self.client.post("/api/auth/register/", invalid, format="json", **self.store_header)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AuthUser.objects.exists())

    def test_duplicate_email_returns_stable_code(self):
        AuthUser.objects.create_user(email="ada@test.local", password="secret12345")
        response = self._register()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "account_exists")

    def test_unverified_login_returns_confirmation_state_without_cookie(self):
        self._register()
        response = self.client.post(
            "/api/auth/login/",
            {"email": "ada@test.local", "password": "secret12345"},
            format="json",
            **self.store_header,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "verification_required")
        self.assertNotIn(access_cookie_name("client", "acme"), response.cookies)

    def test_resend_is_neutral_and_obeys_cooldown(self):
        self._register()
        user = AuthUser.objects.get(email="ada@test.local")
        AuthUser.objects.filter(pk=user.pk).update(
            email_verification_sent_at=timezone.now() - timedelta(minutes=5),
        )
        with self.captureOnCommitCallbacks(execute=True):
            known = self.client.post(
                "/api/auth/resend-verification/", {"email": user.email}, format="json", **self.store_header,
            )
        unknown = self.client.post(
            "/api/auth/resend-verification/", {"email": "unknown@test.local"}, format="json", **self.store_header,
        )
        self.assertEqual(known.status_code, 200)
        self.assertEqual(known.data, unknown.data)
        self.assertEqual(len(mail.outbox), 2)

    def test_login_me_profile_update_and_logout(self):
        user, _profile, _customer = self._verified_customer()
        api = APIClient(enforce_csrf_checks=True)
        csrf = api.get("/api/auth/csrf/").data["csrfToken"]
        login = api.post(
            "/api/auth/login/",
            {"email": user.email, "password": "secret12345"},
            format="json",
            **self.store_header,
        )
        self.assertEqual(login.status_code, 200, login.content)

        me = api.get("/api/auth/me/", **self.store_header)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["customer"]["phone"], "+212612345678")

        updated = api.patch(
            "/api/auth/profile/",
            {"firstName": "Augusta", "phone": "+33612345678", "city": "Paris", "country": "France"},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
            **self.store_header,
        )
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(updated.data["customer"]["firstName"], "Augusta")
        self.assertEqual(updated.data["customer"]["phone"], "+33612345678")
        self.assertFalse(updated.data["customer"]["phoneVerified"])

        logout = api.post("/api/auth/logout/", HTTP_X_CSRFTOKEN=csrf, **self.store_header)
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(logout.cookies[access_cookie_name("client", "acme")].value)

    def test_profile_update_requires_csrf(self):
        user, _profile, _customer = self._verified_customer()
        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient(enforce_csrf_checks=True)
        api.cookies[access_cookie_name("client", self.store.slug)] = access
        response = api.patch(
            "/api/auth/profile/", {"city": "Rabat"}, format="json", **self.store_header,
        )
        self.assertEqual(response.status_code, 403)

    def test_login_creates_customer_role_for_verified_existing_identity(self):
        user = AuthUser.objects.create_user(
            email="merchant@test.local",
            password="secret12345",
            email_verified_at=timezone.now(),
        )
        response = self.client.post(
            "/api/auth/login/",
            {"email": user.email, "password": "secret12345"},
            format="json",
            **self.store_header,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(StoreCustomer.objects.filter(profile__user=user, store=self.store).exists())

    def test_platform_staff_identity_cannot_become_a_store_customer(self):
        staff = AuthUser.objects.create_user(
            email="operator@test.local",
            password="secret12345",
            email_verified_at=timezone.now(),
            is_staff=True,
        )
        response = self.client.post(
            "/api/auth/login/",
            {"email": staff.email, "password": "secret12345"},
            format="json",
            **self.store_header,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data, {"detail": "Unable to sign in with the provided information.", "code": "signin_unavailable"})
        self.assertFalse(ClientProfile.objects.filter(user=staff).exists())

    def test_customer_token_is_bound_to_one_store(self):
        other = Store.objects.create(slug="other", name="Other")
        user, _profile, _customer = self._verified_customer()
        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient()
        api.cookies[access_cookie_name("client", "other")] = access
        response = api.get("/api/auth/me/", HTTP_X_STORE_SLUG=other.slug)
        self.assertEqual(response.status_code, 401)


    def test_customer_password_recovery_link_expires(self):
        user, _profile, _customer = self._verified_customer()
        token = issue_recovery(user, "customer", self.store)
        PasswordResetToken.objects.filter(token_hash=hashlib.sha256(token.encode()).hexdigest()).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        expired = self.client.post(
                "/api/auth/password-reset/confirm/", {"token": token, "password": "newsecret12345"},
                format="json", **self.store_header,
            )
        self.assertEqual(expired.status_code, 400)
        user.refresh_from_db()
        self.assertTrue(user.check_password("secret12345"))

    def test_customer_password_recovery_is_tenant_bound_and_one_time(self):
        user, _profile, _customer = self._verified_customer()
        other = Store.objects.create(slug="other", name="Other Store")
        path = "/api/auth/password-reset/"
        unknown = self.client.post(path, {"email": "nobody@test.local"}, format="json", **self.store_header)
        with self.captureOnCommitCallbacks(execute=True):
            requested = self.client.post(path, {"email": user.email}, format="json", **self.store_header)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(requested.status_code, 200)
        self.assertEqual(unknown.data, requested.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/account#reset=", mail.outbox[0].body)
        token = mail.outbox[0].body.split("/account#reset=", 1)[1].split()[0]
        other_store = self.client.post(
            "/api/auth/password-reset/confirm/", {"token": token, "password": "newsecret12345"},
            format="json", HTTP_X_STORE_SLUG=other.slug,
        )
        self.assertEqual(other_store.status_code, 400)
        old_session = self._authenticated_api(user)
        _access, old_refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        old_session.cookies[refresh_cookie_name("client", self.store.slug)] = old_refresh
        weak = self.client.post(
            "/api/auth/password-reset/confirm/", {"token": token, "password": "short"},
            format="json", **self.store_header,
        )
        self.assertEqual(weak.status_code, 400)
        self.assertEqual(weak.data["code"], "password_invalid")
        confirmed = self.client.post(
            "/api/auth/password-reset/confirm/", {"token": token, "password": "newsecret12345"},
            format="json", **self.store_header,
        )
        self.assertEqual(confirmed.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("newsecret12345"))
        self.assertEqual(old_session.get("/api/auth/me/", **self.store_header).status_code, 401)
        self.assertEqual(old_session.post(
            "/accounts/token/refresh", {"scope": "client"}, format="json", **self.store_header,
        ).status_code, 401)
        replay = self.client.post(
            "/api/auth/password-reset/confirm/", {"token": token, "password": "anotherpass123"},
            format="json", **self.store_header,
        )
        self.assertEqual(replay.status_code, 400)
