"""Account recovery across merchant and customer scopes."""

import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.core import mail
from django.core.cache import cache
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import AuthUser, MerchantProfile, PasswordResetToken
from accounts.password_recovery import issue_recovery
from accounts.tokens import MERCHANT_SCOPE, issue_scoped_tokens
from accounts.cookies import access_cookie_name, refresh_cookie_name
from store.models import ClientProfile, Store, StoreCustomer


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MerchantRecoveryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = AuthUser.objects.create_user(
            email="merchant@test.local", password="oldSecret12345", email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=self.user)

    def test_neutral_response_email_and_one_use_password_change(self):
        unknown = self.client.post("/accounts/password-reset", {"email": "missing@test.local"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            known = self.client.post("/accounts/password-reset", {"email": self.user.email}, format="json")
        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.data, known.data)
        self.assertEqual(len(mail.outbox), 1)
        url = next(part for part in mail.outbox[0].body.split() if "/reset-password#token=" in part)
        token = parse_qs(urlsplit(url).fragment)["token"][0]
        self.assertFalse(PasswordResetToken.objects.filter(token_hash=token).exists())
        stored = PasswordResetToken.objects.get(token_hash=hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(stored.scope, "merchant")
        access, refresh = issue_scoped_tokens(self.user, MERCHANT_SCOPE)
        old_session = APIClient()
        old_session.cookies[access_cookie_name("owner")] = access
        old_session.cookies[refresh_cookie_name("owner")] = refresh
        self.assertEqual(old_session.get("/accounts/me").status_code, 200)

        weak = self.client.post("/accounts/password-reset/confirm", {"token": token, "password": "short"}, format="json")
        self.assertEqual(weak.status_code, 400)
        self.assertEqual(weak.data["code"], "password_invalid")
        good = self.client.post("/accounts/password-reset/confirm", {"token": token, "password": "newSecret12345"}, format="json")
        self.assertEqual(good.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("newSecret12345"))
        self.assertFalse(self.user.check_password("oldSecret12345"))
        self.assertEqual(
            self.client.post("/accounts/token", {"email": self.user.email, "password": "oldSecret12345", "scope": "owner"}, format="json").status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/accounts/token", {"email": self.user.email, "password": "newSecret12345", "scope": "owner"}, format="json").status_code,
            200,
        )
        self.assertEqual(old_session.get("/accounts/me").status_code, 401)
        self.assertEqual(old_session.post("/accounts/token/refresh", {"scope": "owner"}, format="json").status_code, 401)
        self.assertEqual(self.client.post("/accounts/password-reset/confirm", {"token": token, "password": "anotherSecret12345"}, format="json").status_code, 400)

    def test_invalid_email_is_rejected_without_looking_up_accounts(self):
        response = self.client.post("/accounts/password-reset", {"email": "not-an-email"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PasswordResetToken.objects.exists())

    def test_expired_token_and_request_cooldown(self):
        first = issue_recovery(self.user, "merchant")
        self.assertIsNotNone(first)
        self.assertIsNone(issue_recovery(self.user, "merchant"))
        PasswordResetToken.objects.filter(token_hash=hashlib.sha256(first.encode()).hexdigest()).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.client.post("/accounts/password-reset/confirm", {"token": first, "password": "newSecret12345"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_customer_token_cannot_redeem_at_merchant_endpoint(self):
        store = Store.objects.create(slug="other", name="Other")
        customer = AuthUser.objects.create_user(
            email="customer@test.local", password="oldSecret12345", email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=customer, phone="+212612345678")
        StoreCustomer.objects.create(profile=profile, store=store)
        token = issue_recovery(customer, "customer", store)
        response = self.client.post("/accounts/password-reset/confirm", {"token": token, "password": "newSecret12345"}, format="json")
        self.assertEqual(response.status_code, 400)
        customer.refresh_from_db()
        self.assertTrue(customer.check_password("oldSecret12345"))

    def test_mail_failure_remains_neutral_and_is_not_misreported(self):
        with patch("notifications.platform_email.send_merchant_password_reset", return_value=False) as send:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post("/accounts/password-reset", {"email": self.user.email}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("If an eligible account exists", response.data["message"])
        send.assert_called_once()
        self.assertEqual(len(mail.outbox), 0)

    def test_request_endpoint_is_rate_limited(self):
        cache.clear()
        responses = [
            self.client.post("/accounts/password-reset", {"email": f"unknown{n}@test.local"}, format="json")
            for n in range(6)
        ]
        self.assertTrue(all(response.status_code == 200 for response in responses[:5]))
        self.assertEqual(responses[5].status_code, 429)

    def test_customer_recovery_cannot_change_another_stores_shared_credential(self):
        first = Store.objects.create(slug="first", name="First")
        second = Store.objects.create(slug="second", name="Second")
        customer = AuthUser.objects.create_user(
            email="shared@test.local", password="oldSecret12345", email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=customer, phone="+212612345678")
        StoreCustomer.objects.create(profile=profile, store=first)
        StoreCustomer.objects.create(profile=profile, store=second)
        self.assertIsNone(issue_recovery(customer, "customer", first))
        self.assertIsNone(issue_recovery(customer, "customer", second))

    def test_customer_recovery_cannot_change_dual_role_credential(self):
        store = Store.objects.create(slug="dual", name="Dual")
        profile = ClientProfile.objects.create(user=self.user, phone="+212612345678")
        StoreCustomer.objects.create(profile=profile, store=store)
        self.assertIsNone(issue_recovery(self.user, "customer", store))
