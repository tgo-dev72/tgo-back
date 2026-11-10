import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management import call_command
from django.db import OperationalError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import RetailType
from accounts.models import MerchantProfile
from store.models import Store, StoreConfig, StoreMember
from platform_admin.models import PlatformCommerceSettings
from payments.models import PlatformPaymentGateway

from .entitlements import configure_plan_entitlements, resolve_entitlement
from .bootstrap import restore_commercial_reference_data
from .models import EntitlementDefinition, PlanEntitlement, SubscriptionCreditAccount, SubscriptionCreditEntry, SubscriptionLifecycleNotice, SubscriptionPayment, SubscriptionPlan, SubscriptionWebhookEvent, UserSubscription
from .lifecycle import dispatch_subscription_lifecycle_notices
from .policies import BUSINESS_RULES
from .serializers import SubscriptionPlanSerializer
from .services import InvalidSubscriptionWebhook, SubscriptionError, _release_reserved_credit, create_subscription_checkout, ensure_free_subscription, handle_subscription_webhook, refresh_expired_subscription, verify_provider_transaction


User = get_user_model()


class CommercialCatalogRecoveryTests(TestCase):
    def test_restore_is_idempotent_and_rebuilds_translated_plan_promises(self):
        PlanEntitlement.objects.all().delete()
        EntitlementDefinition.objects.all().delete()

        first = restore_commercial_reference_data()
        second = restore_commercial_reference_data()

        self.assertEqual(first["capabilities"], len(BUSINESS_RULES))
        self.assertEqual(second["capabilities_created"], 0)
        self.assertTrue(SubscriptionPlan.objects.filter(slug="free", is_active=True).exists())
        self.assertEqual(EntitlementDefinition.objects.count(), len(BUSINESS_RULES))
        self.assertTrue(all(set(item.translations) == {"en", "fr", "ar"} for item in EntitlementDefinition.objects.all()))
        self.assertEqual(
            PlanEntitlement.objects.filter(plan__slug="free").count(),
            EntitlementDefinition.objects.filter(is_active=True).count(),
        )

        free = SubscriptionPlan.objects.get(slug="free")
        self.assertEqual(set(free.translations), {"en", "fr", "ar"})
        self.assertEqual(free.localized_copy("fr")["name"], "Gratuit")
        self.assertEqual(free.localized_copy("ar")["name"], "مجاني")
        self.assertEqual(SubscriptionPlanSerializer(free).data["translations"], free.translations)
        benefits = SubscriptionPlanSerializer(free).data["benefits"]
        stores = next(item for item in benefits if item["key"] == "stores.max_count")
        self.assertIn("Stores included", stores["translations"]["en"]["display"])
        self.assertIn("Boutiques incluses", stores["translations"]["fr"]["display"])
        self.assertIn("المتاجر المشمولة", stores["translations"]["ar"]["display"])
        self.assertEqual(stores["scope"], "account")
        self.assertIn("Owned stores", stores["counting_rules"])
        marketing = next(item for item in benefits if item["key"] == "marketing.email_monthly_sends")
        self.assertEqual(marketing["reset_window"], "rolling 30 days")
        invoicing = EntitlementDefinition.objects.get(key="invoicing.issue")
        self.assertEqual(invoicing.scope, "store")
        self.assertFalse(
            PlanEntitlement.objects.get(plan=free, definition=invoicing).value
        )
        self.assertEqual(BUSINESS_RULES["invoicing.issue"].operation, "invoicing.issue")

    def test_explicit_free_membership_tracks_live_free_plan_configuration(self):
        user = User.objects.create_user(
            email="free-member@example.com",
            password="pass",
            email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=user)
        PlatformCommerceSettings.objects.create(free_trial_days=21)
        before = timezone.now()
        subscription = ensure_free_subscription(user)
        free = subscription.plan
        store_limit = EntitlementDefinition.objects.get(key="stores.max_count")
        PlanEntitlement.objects.update_or_create(
            plan=free,
            definition=store_limit,
            defaults={"value": 4},
        )

        subscription.refresh_from_db()
        self.assertGreaterEqual(subscription.current_period_end, before + timedelta(days=21))
        self.assertEqual(subscription.entitlements_snapshot, {})
        self.assertEqual(resolve_entitlement(user, "stores.max_count"), 4)

    def test_expired_free_trial_is_not_reactivated_by_status_maintenance(self):
        user = User.objects.create_user(
            email="expired-free@example.com", password="pass", email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=user)
        subscription = ensure_free_subscription(user)
        expired_at = timezone.now() - timedelta(seconds=1)
        UserSubscription.objects.filter(pk=subscription.pk).update(current_period_end=expired_at)

        refreshed = ensure_free_subscription(user)

        self.assertEqual(refreshed.pk, subscription.pk)
        self.assertEqual(refreshed.status, UserSubscription.Status.EXPIRED)
        self.assertEqual(refreshed.current_period_end, expired_at)

    @patch("notifications.platform_email.send_subscription_lifecycle_notice", return_value=True)
    def test_first_expiry_refresh_sends_termination_notice_once(self, send_notice):
        user = User.objects.create_user(
            email="expiry-refresh@example.com", password="pass", email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=user)
        subscription = ensure_free_subscription(user)
        period_end = timezone.now() - timedelta(seconds=1)
        UserSubscription.objects.filter(pk=subscription.pk).update(current_period_end=period_end)
        subscription.refresh_from_db()

        with self.captureOnCommitCallbacks(execute=True):
            refresh_expired_subscription(subscription)
        refresh_expired_subscription(subscription)

        self.assertEqual(send_notice.call_count, 1)
        self.assertTrue(SubscriptionLifecycleNotice.objects.filter(
            subscription=subscription,
            period_end=period_end,
            event=SubscriptionLifecycleNotice.Event.EXPIRED,
        ).exists())

    @patch("notifications.platform_email.send_subscription_lifecycle_notice", return_value=True)
    def test_lifecycle_notices_send_once_at_seven_days_three_days_and_expiry(self, send_notice):
        user = User.objects.create_user(
            email="notices@example.com", password="pass", email_verified_at=timezone.now(),
        )
        MerchantProfile.objects.create(user=user, preferred_language="en")
        subscription = ensure_free_subscription(user)
        now = timezone.now().replace(microsecond=0)

        for remaining, expected in (
            (timedelta(days=7), SubscriptionLifecycleNotice.Event.SEVEN_DAYS),
            (timedelta(days=3), SubscriptionLifecycleNotice.Event.THREE_DAYS),
            (-timedelta(seconds=1), SubscriptionLifecycleNotice.Event.EXPIRED),
        ):
            UserSubscription.objects.filter(pk=subscription.pk).update(
                status=UserSubscription.Status.ACTIVE,
                current_period_end=now + remaining,
            )
            dispatch_subscription_lifecycle_notices(now)
            dispatch_subscription_lifecycle_notices(now)
            self.assertTrue(SubscriptionLifecycleNotice.objects.filter(
                subscription=subscription,
                period_end=now + remaining,
                event=expected,
            ).exists())

        self.assertEqual(send_notice.call_count, 3)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, UserSubscription.Status.EXPIRED)


class FakeTokenAPI:
    """External provider checkout returned by the adapter mock."""

    def __init__(self, token_id="tok_sandbox_123"):
        self.provider_token = token_id
        self.payment_url = f"https://youcanpay.com/sandbox/payment-form/{token_id}?lang=ar"


@override_settings(
    PLATFORM_PAYMENT_PRIVATE_KEY="private_test_key",
    PLATFORM_PAYMENT_PUBLIC_KEY="public_test_key",
    PLATFORM_PAYMENT_ACCESS_TOKEN="oauth_test_token",
    PLATFORM_PAYMENT_SANDBOX=True,
    MERCHANT_DASHBOARD_URL="https://dash.example.test",
    CORS_ALLOWED_ORIGINS=["https://localhost:5179", "https://dash.example.test"],
    CSRF_TRUSTED_ORIGINS=["https://localhost:5179", "https://dash.example.test"],
)
class SubscriptionFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="merchant@example.com", password="pass")
        cls.other = User.objects.create_user(email="other@example.com", password="pass")
        retail = RetailType.objects.create(key="subscription-test", label="Subscription test")
        cls.store_a = Store.objects.create(name="A", slug="store-a", retail_type=retail)
        cls.store_b = Store.objects.create(name="B", slug="store-b", retail_type=retail)
        StoreMember.objects.create(user=cls.user, store=cls.store_a, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(user=cls.user, store=cls.store_b, role=StoreMember.Roles.OWNER)
        cls.free = SubscriptionPlan.objects.get(slug="free")
        cls.premium = SubscriptionPlan.objects.get(slug="premium")
        cls.premium.price = Decimal("99.00")
        cls.premium.is_active = True
        cls.premium.grants_premium_designs = True
        cls.premium.save()
        cls.single = SubscriptionPlan.objects.create(
            name="Single store", slug="single-store", price="49.00", currency="MAD",
            duration_days=30, entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
            grants_premium_designs=True,
        )
        cls.single.refresh_from_db()
        cls.alternate = SubscriptionPlan.objects.create(
            name="Growth", slug="growth-test", price="129.00", currency=cls.premium.currency,
            duration_days=45,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        cls.alternate.refresh_from_db()

    def authenticated_client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def setUp(self):
        self.external_checkout_patcher = patch(
            "payments.gateways.youcanpay.YouCanPayGateway._create_external_checkout",
            return_value=FakeTokenAPI(),
        )
        self.external_checkout_mock = self.external_checkout_patcher.start()
        self.addCleanup(self.external_checkout_patcher.stop)
        self.provider_get_patcher = patch("requests.get")
        self.provider_get_mock = self.provider_get_patcher.start()
        self.addCleanup(self.provider_get_patcher.stop)

        def provider_transaction(url, **_kwargs):
            transaction_id = url.rstrip("/").rsplit("/", 1)[-1]
            payment_id = transaction_id.removeprefix("txn_")
            try:
                payment = SubscriptionPayment.objects.get(pk=payment_id)
            except (SubscriptionPayment.DoesNotExist, ValueError):
                return SimpleNamespace(status_code=404, json=lambda: {})
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "id": transaction_id,
                    "status": "paid",
                    "is_live_mode": False,
                    "amount": {"amount": str(payment.amount), "currency": payment.currency},
                    "order_id": str(payment.pk),
                    "metadata": {"payment_id": str(payment.pk)},
                },
            )

        self.provider_get_mock.side_effect = provider_transaction

    def payload(
        self,
        payment,
        *,
        event="transaction.paid",
        status=1,
        amount=None,
        token=None,
        event_id=None,
        transaction_id=None,
    ):
        return {
            "id": event_id or f"evt_{payment.id}",
            "event_name": event,
            "sandbox": True,
            "payload": {
                "token": {"id": token or payment.provider_token},
                "metadata": {
                    "purpose": "platform_subscription",
                    "payment_id": str(payment.id),
                },
                "transaction": {
                    "id": transaction_id or f"txn_{payment.id}", "order_id": str(payment.id),
                    "amount": amount if amount is not None else int(payment.amount * 100),
                    "currency": payment.currency, "status": status,
                },
            },
        }

    @patch("payments.gateways.youcanpay.YouCanPayGateway._create_external_checkout")
    def test_checkout_snapshots_terms_and_returns_external_payment_form(self, client_factory):
        fake = FakeTokenAPI()
        client_factory.return_value = fake
        payment, url = create_subscription_checkout(
            self.user, self.premium, lang="fr", return_origin="https://localhost:5179",
        )
        self.assertEqual(payment.amount, Decimal("99.00"))
        self.assertEqual(payment.gross_amount, Decimal("99.00"))
        self.assertEqual(payment.duration_days, self.premium.duration_days)
        self.assertEqual(url, "https://youcanpay.com/sandbox/payment-form/tok_sandbox_123?lang=ar")
        self.assertNotIn(str(payment.pk), url)
        checkout_request = client_factory.call_args.args[0]
        self.assertTrue(checkout_request.provider_hosted)
        self.assertTrue(checkout_request.success_url.startswith("https://localhost:5179/subscription?"))

    @patch("payments.gateways.youcanpay.YouCanPayGateway._create_external_checkout")
    def test_existing_external_checkout_is_reused_with_current_language(self, client_factory):
        client_factory.return_value = FakeTokenAPI()
        payment, _url = create_subscription_checkout(self.user, self.premium, lang="fr")
        payment.checkout_url = f"https://youcanpay.com/payment-form/{payment.provider_token}?lang=fr"
        payment.save(update_fields=["checkout_url", "updated_at"])
        client_factory.return_value = FakeTokenAPI("tok_replacement")

        resumed, repaired_url = create_subscription_checkout(self.user, self.premium, lang="ar")

        self.assertEqual(resumed.pk, payment.pk)
        self.assertEqual(
            repaired_url,
            f"https://youcanpay.com/sandbox/payment-form/{payment.provider_token}?lang=ar",
        )
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        client_factory.assert_called_once()

    @patch("payments.gateways.youcanpay.YouCanPayGateway._create_external_checkout")
    def test_checkout_has_no_backend_hosted_payment_page(self, client_factory):
        client_factory.return_value = FakeTokenAPI("tok_browser_page")
        payment, payment_url = create_subscription_checkout(
            self.user,
            self.premium,
            lang="ar",
            return_origin="https://dash.example.test",
        )

        self.assertEqual(
            payment_url,
            "https://youcanpay.com/sandbox/payment-form/tok_browser_page?lang=ar",
        )
        self.assertEqual(payment.checkout_url, payment_url)
        self.assertNotIn("/api/subscriptions/checkout/", payment_url)

    def test_upgrade_reserves_existing_balance_and_grants_unused_period_after_payment(self):
        now = timezone.now()
        UserSubscription.objects.create(
            user=self.user,
            plan=self.premium,
            status=UserSubscription.Status.ACTIVE,
            started_at=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("20.00"),
        )

        payment, _url = create_subscription_checkout(self.user, self.alternate, use_balance=True)

        self.assertEqual(payment.gross_amount, Decimal("129.00"))
        self.assertEqual(payment.credit_reserved, Decimal("20.00"))
        self.assertEqual(payment.amount, Decimal("109.00"))
        self.assertEqual(payment.proration_credit, Decimal("46"))
        self.assertEqual(payment.proration_snapshot["remaining_days"], 14)
        self.assertEqual(payment.proration_snapshot["rounding"], "whole_currency_unit_down")
        self.assertTrue(payment.balance_authorized)
        account.refresh_from_db()
        self.assertEqual(account.balance, Decimal("20.00"))
        self.assertEqual(account.reserved, Decimal("20.00"))

        payload = self.payload(payment)
        handle_subscription_webhook(payment, payload)
        handle_subscription_webhook(payment, payload)

        account.refresh_from_db(); payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(account.reserved, Decimal("0.00"))
        self.assertEqual(account.balance, payment.proration_credit)
        self.assertEqual(SubscriptionCreditEntry.objects.filter(payment=payment).count(), 2)

    def test_lower_priced_plan_cannot_replace_active_plan_immediately(self):
        UserSubscription.objects.create(
            user=self.user,
            plan=self.alternate,
            status=UserSubscription.Status.ACTIVE,
            started_at=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=45),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )

        with self.assertRaisesRegex(SubscriptionError, "cannot replace"):
            create_subscription_checkout(self.user, self.premium, use_balance=True)

        self.assertFalse(SubscriptionPayment.objects.filter(user=self.user).exists())
        self.external_checkout_mock.assert_not_called()

    def test_failed_upgrade_releases_balance_without_posting_ledger_entries(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("20.00"),
        )
        payment, _url = create_subscription_checkout(self.user, self.premium, use_balance=True)

        handle_subscription_webhook(
            payment,
            self.payload(payment, event="transaction.failed", status=0),
        )

        account.refresh_from_db(); payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.FAILED)
        self.assertEqual(account.balance, Decimal("20.00"))
        self.assertEqual(account.reserved, Decimal("0.00"))
        self.assertFalse(SubscriptionCreditEntry.objects.filter(payment=payment).exists())

    def test_releasing_one_checkout_twice_cannot_release_another_checkout_reservation(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("30.00"), reserved=Decimal("30.00"),
        )
        first = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            gross_amount=self.premium.price, amount=Decimal("89.00"), credit_reserved=Decimal("10.00"),
            currency=self.premium.currency, duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )

        _release_reserved_credit(first)
        _release_reserved_credit(first)

        account.refresh_from_db(); first.refresh_from_db()
        self.assertEqual(account.reserved, Decimal("20.00"))
        self.assertIsNotNone(first.credit_released_at)

    def test_balance_is_never_spent_across_currencies(self):
        SubscriptionCreditAccount.objects.create(
            user=self.user, currency="MAD", balance=Decimal("50.00"),
        )
        euro_plan = SubscriptionPlan.objects.create(
            name="Europe", slug="europe-test", price=Decimal("20.00"), currency="EUR",
            duration_days=30,
        )

        payment, _url = create_subscription_checkout(self.user, euro_plan, use_balance=True)

        self.assertEqual(payment.amount, Decimal("20.00"))
        self.assertEqual(payment.credit_reserved, Decimal("0.00"))

    def test_balance_can_fully_cover_upgrade_without_external_checkout(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=self.premium.price,
        )

        payment, url = create_subscription_checkout(self.user, self.premium, use_balance=True)

        payment.refresh_from_db(); account.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(account.balance, Decimal("0.00"))
        self.assertEqual(account.reserved, Decimal("0.00"))
        self.assertIn("payment=success", url)
        self.external_checkout_mock.assert_not_called()

    def test_balance_larger_than_pack_consumes_only_pack_price(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("500.00"),
        )

        payment, url = create_subscription_checkout(self.user, self.premium, use_balance=True)

        payment.refresh_from_db(); account.refresh_from_db()
        self.assertEqual(payment.gross_amount, Decimal("99.00"))
        self.assertEqual(payment.credit_reserved, Decimal("99.00"))
        self.assertEqual(payment.amount, Decimal("0.00"))
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(account.balance, Decimal("401.00"))
        self.assertEqual(account.reserved, Decimal("0.00"))
        self.assertIn("payment=success", url)
        self.assertTrue(SubscriptionCreditEntry.objects.filter(
            payment=payment,
            entry_type=SubscriptionCreditEntry.EntryType.CHECKOUT_APPLIED,
            amount=Decimal("-99.00"),
            balance_after=Decimal("401.00"),
        ).exists())
        self.external_checkout_mock.assert_not_called()

    def test_balance_is_not_used_without_explicit_consent(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("20.00"),
        )

        payment, _url = create_subscription_checkout(self.user, self.premium)

        account.refresh_from_db()
        self.assertFalse(payment.balance_authorized)
        self.assertEqual(payment.credit_reserved, Decimal("0.00"))
        self.assertEqual(payment.amount, self.premium.price)
        self.assertEqual(account.reserved, Decimal("0.00"))

    def test_legacy_fractional_balance_is_rounded_down_before_use(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user, currency=self.premium.currency, balance=Decimal("20.75"),
        )

        payment, _url = create_subscription_checkout(self.user, self.premium, use_balance=True)

        account.refresh_from_db()
        self.assertEqual(payment.credit_reserved, Decimal("20"))
        self.assertEqual(payment.amount, Decimal("79.00"))
        self.assertEqual(account.reserved, Decimal("20"))

    def test_checkout_entitlements_remain_immutable_after_plan_edit(self):
        limit = EntitlementDefinition.objects.create(
            key="catalog.products_limit", name="Product limit", value_type="integer",
            scope="store", default_value=25,
        )
        configure_plan_entitlements(self.premium, {limit.key: 200})
        payment, _url = create_subscription_checkout(self.user, self.premium)

        configure_plan_entitlements(self.premium, {limit.key: 500})
        payment.refresh_from_db()

        self.assertEqual(payment.entitlements_snapshot[limit.key], 200)
        self.assertNotEqual(payment.plan_revision, self.premium.revision)

    def test_resolver_combines_active_subscription_limits_by_scope(self):
        limit = EntitlementDefinition.objects.create(
            key="catalog.products_limit", name="Product limit", value_type="integer",
            scope="store", default_value=25,
        )
        UserSubscription.objects.create(
            user=self.user, plan=self.single, selected_store=self.store_a,
            status=UserSubscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
            entitlements_snapshot={limit.key: 300},
        )

        self.assertEqual(resolve_entitlement(self.user, limit.key, self.store_a), 300)
        self.assertEqual(resolve_entitlement(self.user, limit.key, self.store_b), 25)

    def test_canceled_checkout_webhook_cannot_reactivate_subscription(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name, amount=self.premium.price,
            currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_reset", status=SubscriptionPayment.Status.CANCELED,
        )

        handle_subscription_webhook(payment, self.payload(payment))

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CANCELED)
        self.assertFalse(UserSubscription.objects.filter(user=self.user, status=UserSubscription.Status.ACTIVE).exists())
        self.assertEqual(SubscriptionWebhookEvent.objects.get(payment=payment).status, SubscriptionWebhookEvent.Status.IGNORED)

    def test_free_reset_cancels_current_access_and_unfinished_checkouts(self):
        self.user.email_verified_at = timezone.now()
        self.user.save(update_fields=["email_verified_at"])
        MerchantProfile.objects.create(user=self.user)
        subscription = UserSubscription.objects.create(
            user=self.user, plan=self.premium, status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name, amount=self.premium.price,
            currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_pending",
        )
        StoreConfig.objects.filter(store__in=[self.store_a, self.store_b]).update(plan=StoreConfig.Plans.PREMIUM)

        call_command("reset_subscriptions_to_free", apply=True)

        subscription.refresh_from_db(); payment.refresh_from_db()
        self.assertEqual(subscription.status, UserSubscription.Status.CANCELED)
        self.assertEqual(payment.status, SubscriptionPayment.Status.CANCELED)
        free_membership = UserSubscription.objects.get(
            user=self.user,
            status=UserSubscription.Status.ACTIVE,
        )
        self.assertEqual(free_membership.plan.slug, "free")
        self.assertGreater(free_membership.current_period_end, timezone.now())
        self.assertFalse(StoreConfig.objects.filter(store__in=[self.store_a, self.store_b]).exclude(plan=StoreConfig.Plans.FREE).exists())

    def test_repeated_checkout_reuses_one_payment_and_provider_token(self):

        first, first_url = create_subscription_checkout(self.user, self.premium)
        second, second_url = create_subscription_checkout(self.user, self.premium)

        self.assertEqual(second.pk, first.pk)
        self.assertEqual(second_url, first_url)
        self.assertEqual(SubscriptionPayment.objects.filter(user=self.user).count(), 1)
        self.external_checkout_mock.assert_called_once()

    def test_fresh_tokenless_checkout_reservation_is_not_stolen(self):
        reservation = SubscriptionPayment.objects.create(
            user=self.user,
            plan=self.premium,
            plan_name=self.premium.name,
            amount=self.premium.price,
            currency=self.premium.currency,
            duration_days=self.premium.duration_days,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )

        with self.assertRaisesRegex(SubscriptionError, "still being prepared"):
            create_subscription_checkout(self.user, self.premium)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(SubscriptionPayment.objects.filter(user=self.user).count(), 1)
        self.external_checkout_mock.assert_not_called()

    @override_settings(SUBSCRIPTION_CHECKOUT_INITIALIZATION_TIMEOUT_SECONDS=30)
    def test_abandoned_tokenless_reservation_is_recovered(self):
        reservation = SubscriptionPayment.objects.create(
            user=self.user,
            plan=self.premium,
            plan_name=self.premium.name,
            amount=self.premium.price,
            currency=self.premium.currency,
            duration_days=self.premium.duration_days,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        SubscriptionPayment.objects.filter(pk=reservation.pk).update(
            updated_at=timezone.now() - timedelta(minutes=2),
        )

        replacement, _url = create_subscription_checkout(self.user, self.premium)

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, SubscriptionPayment.Status.FAILED)
        self.assertNotEqual(replacement.pk, reservation.pk)
        self.assertEqual(replacement.provider_token, "tok_sandbox_123")

    @override_settings(SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS=300)
    def test_expired_provider_session_is_replaced_with_a_fresh_token(self):
        stale = SubscriptionPayment.objects.create(
            user=self.user,
            plan=self.premium,
            plan_name=self.premium.name,
            gross_amount=self.premium.price,
            amount=self.premium.price,
            currency=self.premium.currency,
            duration_days=self.premium.duration_days,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_expired",
            checkout_url="https://youcanpay.com/sandbox/payment-form/tok_expired?lang=en",
        )
        SubscriptionPayment.objects.filter(pk=stale.pk).update(
            updated_at=timezone.now() - timedelta(minutes=10),
        )

        replacement, url = create_subscription_checkout(self.user, self.premium)

        stale.refresh_from_db()
        self.assertEqual(stale.status, SubscriptionPayment.Status.FAILED)
        self.assertNotEqual(replacement.pk, stale.pk)
        self.assertEqual(url, "https://youcanpay.com/sandbox/payment-form/tok_sandbox_123?lang=ar")
        self.assertEqual(replacement.provider_token, "tok_sandbox_123")

    def test_different_plan_cannot_open_a_second_checkout_for_same_scope(self):
        first, _url = create_subscription_checkout(self.user, self.premium)

        with self.assertRaises(SubscriptionError):
            create_subscription_checkout(self.user, self.alternate)

        self.assertEqual(first.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(SubscriptionPayment.objects.filter(user=self.user).count(), 1)
        self.external_checkout_mock.assert_called_once()

    def test_pending_checkout_cannot_be_reused_with_a_different_balance_choice(self):
        SubscriptionCreditAccount.objects.create(
            user=self.user, currency="MAD", balance=Decimal("20.00"),
        )
        first, _url = create_subscription_checkout(self.user, self.premium, use_balance=False)

        with self.assertRaises(SubscriptionError):
            create_subscription_checkout(self.user, self.premium, use_balance=True)

        first.refresh_from_db()
        self.assertFalse(first.balance_authorized)
        self.assertEqual(first.credit_reserved, Decimal("0.00"))
        self.assertEqual(SubscriptionPayment.objects.filter(user=self.user).count(), 1)
        self.external_checkout_mock.assert_called_once()

        response = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id, "use_balance": True},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "checkout_in_progress")
        self.assertEqual(response.data["checkout"]["payment_id"], str(first.id))
        self.assertFalse(response.data["checkout"]["balance_authorized"])
        self.assertGreater(response.data["checkout"]["retry_after_seconds"], 0)

    def test_idempotency_key_cannot_be_reused_for_another_purchase(self):
        checkout_key = uuid.uuid4()
        create_subscription_checkout(
            self.user,
            self.premium,
            idempotency_key=checkout_key,
        )

        with self.assertRaises(SubscriptionError):
            create_subscription_checkout(
                self.user,
                self.single,
                store_id=self.store_a.id,
                idempotency_key=checkout_key,
            )

        self.assertEqual(SubscriptionPayment.objects.filter(user=self.user).count(), 1)

    def test_checkout_rejects_plan_that_is_already_active(self):
        UserSubscription.objects.create(
            user=self.user,
            plan=self.premium,
            status=UserSubscription.Status.ACTIVE,
            started_at=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )

        response = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(SubscriptionPayment.objects.filter(user=self.user).exists())
        self.external_checkout_mock.assert_not_called()

    def test_checkout_allows_same_plan_renewal_during_final_seven_days(self):
        period_end = timezone.now() + timedelta(days=6)
        subscription = UserSubscription.objects.create(
            user=self.user, plan=self.premium, status=UserSubscription.Status.ACTIVE,
            started_at=timezone.now() - timedelta(days=28), current_period_end=period_end,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )
        response = self.authenticated_client().post(
            reverse("subscription-checkout"), {"plan_id": self.premium.id}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        payment = SubscriptionPayment.objects.get(pk=response.data["payment_id"])
        self.assertTrue(payment.proration_snapshot["renewal"])
        self.assertEqual(payment.proration_credit, Decimal("0.00"))
        handle_subscription_webhook(payment, self.payload(payment))
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, period_end + timedelta(days=self.premium.duration_days))

    def test_checkout_rejects_untrusted_return_origin_before_tokenization(self):
        before = SubscriptionPayment.objects.count()
        response = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id, "return_origin": "https://evil.example"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(SubscriptionPayment.objects.count(), before)
        self.external_checkout_mock.assert_not_called()

    def test_one_store_checkout_requires_owned_store(self):
        response = self.authenticated_client().post(
            reverse("subscription-checkout"), {"plan_id": self.single.id}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        response = self.authenticated_client().post(
            reverse("subscription-checkout"), {"plan_id": self.single.id, "store_id": self.store_a.id}, format="json",
        )
        self.assertEqual(response.status_code, 201)

    def test_public_catalog_hides_inactive_packs_and_reports_sandbox(self):
        self.premium.is_active = False
        self.premium.save(update_fields=["is_active"])
        with self.assertNumQueries(4):
            response = APIClient().get(reverse("subscription-plans"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("premium", {item["slug"] for item in response.data["plans"]})
        self.assertTrue(response.data["billing"]["available"])
        self.assertTrue(response.data["billing"]["sandbox"])
        self.assertIn("max-age=0", response["Cache-Control"])
        self.assertIn("must-revalidate", response["Cache-Control"])
        self.assertEqual(response.data["billing"]["gateways"][0]["code"], "youcanpay")

    def test_disabled_gateway_is_hidden_and_rejected_for_checkout(self):
        PlatformPaymentGateway.objects.create(code="youcanpay", is_enabled=False)
        catalog = APIClient().get(reverse("subscription-plans"))
        checkout = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id, "provider": "youcanpay"},
            format="json",
        )
        self.assertEqual(catalog.data["billing"]["gateways"], [])
        self.assertEqual(checkout.status_code, 409)
        self.assertFalse(SubscriptionPayment.objects.filter(user=self.user).exists())

    @override_settings(
        PLATFORM_PAYMENT_ACCESS_TOKEN="",
        PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=False,
    )
    def test_catalog_keeps_checkout_available_without_optional_oauth(self):
        response = APIClient().get(reverse("subscription-plans"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["billing"]["available"])

    @override_settings(
        PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=True,
        PLATFORM_PAYMENT_ACCESS_TOKEN="",
    )
    def test_catalog_disables_checkout_when_required_provider_verification_is_missing(self):
        response = APIClient().get(reverse("subscription-plans"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["billing"]["available"])

    def test_merchant_can_upgrade_before_creating_first_store(self):
        merchant = User.objects.create_user(email="new-merchant@example.com", password="pass")
        MerchantProfile.objects.create(user=merchant)
        client = APIClient()
        client.force_authenticate(merchant)

        status_response = client.get(reverse("subscription-status"))
        checkout_response = client.post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id, "return_origin": "https://localhost:5179"},
            format="json",
        )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["subscription"]["plan"]["slug"], "free")
        self.assertGreater(status_response.data["subscription"]["current_period_end"], timezone.now())
        self.assertEqual(checkout_response.status_code, 201, checkout_response.data)
        self.assertTrue(SubscriptionPayment.objects.filter(user=merchant).exists())

    @override_settings(
        DEBUG=True,
        DEV_API_URL="https://127.0.0.1:8001",
        ALLOWED_HOSTS=["127.0.0.1"],
        CORS_ALLOWED_ORIGINS=[],
        CSRF_TRUSTED_ORIGINS=[],
    )
    def test_dev_checkout_accepts_current_private_host_without_static_allow_list(self):
        current_origin = "https://10.189.110.138:5179"

        response = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {
                "plan_id": self.premium.id,
                "return_origin": current_origin,
            },
            format="json",
            HTTP_HOST="127.0.0.1:8001",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["payment_url"].startswith("https://youcanpay.com/sandbox/payment-form/"))
        self.assertNotIn("127.0.0.1", response.data["payment_url"])
        checkout_request = self.external_checkout_mock.call_args.args[0]
        self.assertTrue(checkout_request.success_url.startswith(f"{current_origin}/subscription?"))

    def test_invited_team_member_cannot_manage_account_subscription(self):
        team_user = User.objects.create_user(email="team@example.com", password="pass")
        StoreMember.objects.create(
            user=team_user,
            store=self.store_a,
            role=StoreMember.Roles.ADMIN,
        )
        client = APIClient()
        client.force_authenticate(team_user)

        self.assertEqual(client.get(reverse("subscription-status")).status_code, 403)

    def test_signed_webhook_activates_all_owned_stores_and_is_idempotent(self):
        free_subscription = ensure_free_subscription(self.user)
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name, amount=self.premium.price,
            currency="MAD", duration_days=30, entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True, provider_token="tok_paid",
        )
        payload = self.payload(payment)
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"private_test_key", raw, hashlib.sha256).hexdigest()
        with patch("notifications.platform_email.send_subscription_activated") as send_activated:
            with self.captureOnCommitCallbacks(execute=True):
                response = APIClient().post(
                    reverse("subscription-webhook"), data=raw, content_type="application/json",
                    HTTP_X_YOUCANPAY_SIGNATURE=signature,
                )
            send_activated.assert_called_once()
        self.assertEqual(response.status_code, 200)
        subscription = UserSubscription.objects.get(user=self.user)
        self.assertEqual(subscription.pk, free_subscription.pk)
        self.assertEqual(subscription.plan, self.premium)
        first_end = subscription.current_period_end
        self.assertEqual(subscription.status, UserSubscription.Status.ACTIVE)
        self.assertFalse(StoreConfig.objects.filter(store__in=[self.store_a, self.store_b], plan=StoreConfig.Plans.FREE).exists())
        handle_subscription_webhook(payment, payload)
        subscription.refresh_from_db()
        self.assertEqual(subscription.current_period_end, first_end)
        self.assertEqual(SubscriptionWebhookEvent.objects.count(), 1)

    @override_settings(
        PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=True,
        PLATFORM_PAYMENT_ACCESS_TOKEN="oauth_test_token",
    )
    @patch("requests.get")
    def test_paid_webhook_is_confirmed_against_provider_transaction_api(self, provider_get):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_verified",
        )
        payload = self.payload(payment)
        provider_get.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": payload["payload"]["transaction"]["id"],
                "status": "paid",
                "is_live_mode": False,
                "amount": {"amount": str(payment.amount), "currency": payment.currency},
                "order_id": str(payment.pk),
                "metadata": {"payment_id": str(payment.pk)},
            },
        )
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"private_test_key", raw, hashlib.sha256).hexdigest()

        response = APIClient().post(
            reverse("subscription-webhook"),
            data=raw,
            content_type="application/json",
            HTTP_X_YOUCANPAY_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 200, response.data)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        provider_get.assert_called_once()
        self.assertEqual(provider_get.call_args.kwargs["timeout"], 10)

    @override_settings(
        PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=True,
        PLATFORM_PAYMENT_ACCESS_TOKEN="",
    )
    def test_paid_webhook_returns_retryable_error_when_provider_truth_is_unavailable(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_needs_truth",
        )
        payload = self.payload(payment)
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"private_test_key", raw, hashlib.sha256).hexdigest()

        response = APIClient().post(
            reverse("subscription-webhook"),
            data=raw,
            content_type="application/json",
            HTTP_X_YOUCANPAY_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, 503)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertFalse(UserSubscription.objects.filter(user=self.user).exists())

    @override_settings(PLATFORM_PAYMENT_ACCESS_TOKEN="oauth_test_token")
    @patch("requests.get")
    def test_provider_readback_rejects_a_different_order(self, provider_get):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_wrong_readback",
        )
        payload = self.payload(payment)
        provider_get.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": payload["payload"]["transaction"]["id"],
                "status": "paid",
                "is_live_mode": False,
                "amount": {"amount": str(payment.amount), "currency": payment.currency},
                "order_id": str(uuid.uuid4()),
                "metadata": {},
            },
        )

        with self.assertRaisesRegex(InvalidSubscriptionWebhook, "does not match"):
            verify_provider_transaction(payment, payload)

    def test_webhook_rejects_wrong_environment_and_order(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_binding",
        )
        wrong_environment = self.payload(payment)
        wrong_environment["sandbox"] = False
        with self.assertRaisesRegex(InvalidSubscriptionWebhook, "environment"):
            handle_subscription_webhook(payment, wrong_environment)

        wrong_order = self.payload(payment, event_id="evt_wrong_order")
        wrong_order["payload"]["transaction"]["order_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(InvalidSubscriptionWebhook, "order"):
            handle_subscription_webhook(payment, wrong_order)

        self.assertFalse(SubscriptionWebhookEvent.objects.filter(payment=payment).exists())

    def test_webhook_event_id_cannot_be_replayed_with_mutated_payload(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_replay",
        )
        payload = self.payload(payment, event="transaction.created", status=0)
        handle_subscription_webhook(payment, payload)
        mutated = self.payload(payment, event="transaction.created", status=0)
        mutated["payload"]["metadata"]["unexpected"] = True

        with self.assertRaisesRegex(InvalidSubscriptionWebhook, "reused"):
            handle_subscription_webhook(payment, mutated)

    def test_converted_transaction_uses_original_base_amount_and_currency(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount="10.00", currency="EUR", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_converted",
        )
        payload = self.payload(payment, amount=1100)
        payload["payload"]["transaction"].update({
            "currency": "MAD",
            "base_amount": 1000,
            "base_currency": "EUR",
        })

        handle_subscription_webhook(payment, payload)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)

    def test_repeated_failure_events_send_only_one_notification(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_failed",
        )
        with patch("notifications.platform_email.send_subscription_payment_failed") as send_failed:
            with self.captureOnCommitCallbacks(execute=True):
                handle_subscription_webhook(
                    payment,
                    self.payload(payment, event="transaction.failed", status=0, event_id="evt_failed_1"),
                )
            with self.captureOnCommitCallbacks(execute=True):
                handle_subscription_webhook(
                    payment,
                    self.payload(payment, event="transaction.failed", status=0, event_id="evt_failed_2"),
                )
            send_failed.assert_called_once()

        self.assertEqual(
            SubscriptionWebhookEvent.objects.get(event_id="evt_failed_2").status,
            SubscriptionWebhookEvent.Status.IGNORED,
        )

    def test_two_distinct_paid_checkouts_do_not_extend_same_active_plan(self):
        payments = [
            SubscriptionPayment.objects.create(
                user=self.user,
                plan=self.premium,
                plan_name=self.premium.name,
                amount=self.premium.price,
                currency="MAD",
                duration_days=30,
                entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
                grants_premium_designs=True,
                provider_token=f"tok_paid_{index}",
            )
            for index in (1, 2)
        ]

        handle_subscription_webhook(payments[0], self.payload(payments[0]))
        subscription = UserSubscription.objects.get(user=self.user)
        first_end = subscription.current_period_end
        handle_subscription_webhook(payments[1], self.payload(payments[1]))

        subscription.refresh_from_db()
        payments[1].refresh_from_db()
        self.assertEqual(subscription.current_period_end, first_end)
        self.assertEqual(payments[1].status, SubscriptionPayment.Status.DUPLICATE)

    def test_delayed_older_payment_cannot_replace_a_newer_confirmed_plan(self):
        older = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency=self.premium.currency, duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_older",
        )
        newer = SubscriptionPayment.objects.create(
            user=self.user, plan=self.alternate, plan_name=self.alternate.name,
            amount=self.alternate.price, currency=self.alternate.currency, duration_days=45,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_newer",
        )

        handle_subscription_webhook(newer, self.payload(newer))
        handle_subscription_webhook(older, self.payload(older))

        subscription = UserSubscription.objects.get(
            user=self.user,
            status=UserSubscription.Status.ACTIVE,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        older.refresh_from_db()
        self.assertEqual(subscription.plan_id, self.alternate.id)
        self.assertEqual(older.status, SubscriptionPayment.Status.DUPLICATE)

    def test_status_correlates_returned_payment_without_exposing_another_user(self):
        requested = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency=self.premium.currency, duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_requested",
        )
        latest = SubscriptionPayment.objects.create(
            user=self.user, plan=self.alternate, plan_name=self.alternate.name,
            amount=self.alternate.price, currency=self.alternate.currency, duration_days=45,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_latest",
        )

        response = self.authenticated_client().get(
            reverse("subscription-status"), {"payment_id": str(requested.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["payment"]["id"], str(requested.pk))
        self.assertEqual(response.data["latest_payment"]["id"], str(latest.pk))

        other_payment = SubscriptionPayment.objects.create(
            user=self.other, plan=self.premium, plan_name=self.premium.name,
            amount=self.premium.price, currency=self.premium.currency, duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_private",
        )
        private_response = self.authenticated_client().get(
            reverse("subscription-status"), {"payment_id": str(other_payment.pk)},
        )
        self.assertIsNone(private_response.data["payment"])

    def test_status_retries_one_transient_database_disconnect(self):
        client = self.authenticated_client()
        from subscriptions.views import SubscriptionStatusView

        original_get = SubscriptionStatusView.get
        attempts = 0

        def flaky_get(view, request, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError("connection closed")
            return original_get(view, request, *args, **kwargs)

        with patch.object(SubscriptionStatusView, "get", new=flaky_get):
            response = client.get(reverse("subscription-status"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 2)

    def test_status_keeps_reserved_credit_visible_as_part_of_total_balance(self):
        SubscriptionCreditAccount.objects.create(
            user=self.user,
            currency="MAD",
            balance=Decimal("20.00"),
            reserved=Decimal("20.00"),
        )

        response = self.authenticated_client().get(reverse("subscription-status"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["balances"], [{
            "currency": "MAD",
            "balance": "20.00",
            "reserved": "20.00",
            "available": "0",
        }])

    @override_settings(SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS=300)
    def test_status_releases_balance_from_expired_checkout(self):
        account = SubscriptionCreditAccount.objects.create(
            user=self.user,
            currency="MAD",
            balance=Decimal("20.00"),
            reserved=Decimal("20.00"),
        )
        payment = SubscriptionPayment.objects.create(
            user=self.user,
            plan=self.premium,
            plan_name=self.premium.name,
            gross_amount=self.premium.price,
            amount=Decimal("79.00"),
            balance_authorized=True,
            credit_reserved=Decimal("20.00"),
            currency="MAD",
            duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            provider_token="tok_abandoned",
        )
        SubscriptionPayment.objects.filter(pk=payment.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6),
        )

        response = self.authenticated_client().get(reverse("subscription-status"))

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.FAILED)
        self.assertIsNotNone(payment.credit_released_at)
        self.assertEqual(account.balance, Decimal("20.00"))
        self.assertEqual(account.reserved, Decimal("0.00"))
        self.assertEqual(response.data["balances"][0]["available"], "20")

    def test_unrelated_webhook_event_is_recorded_and_ignored(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user,
            plan=self.premium,
            plan_name=self.premium.name,
            amount=self.premium.price,
            currency="MAD",
            duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
            provider_token="tok_ignored",
        )
        payload = self.payload(payment, event="transaction.created", status=0)

        handle_subscription_webhook(payment, payload)

        payment.refresh_from_db()
        event = SubscriptionWebhookEvent.objects.get(event_id=payload["id"])
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)
        self.assertEqual(event.status, SubscriptionWebhookEvent.Status.IGNORED)

    def test_one_store_entitlement_only_upgrades_selected_store(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.single, selected_store=self.store_a, plan_name=self.single.name,
            amount=self.single.price, currency="MAD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
            grants_premium_designs=True, provider_token="tok_single",
        )
        handle_subscription_webhook(payment, self.payload(payment))
        self.store_a.config.refresh_from_db()
        self.store_b.config.refresh_from_db()
        self.assertEqual(self.store_a.config.plan, StoreConfig.Plans.PREMIUM)
        self.assertEqual(self.store_b.config.plan, StoreConfig.Plans.FREE)

    def test_store_scoped_packs_can_coexist_for_multiple_stores(self):
        for index, store in enumerate((self.store_a, self.store_b), start=1):
            payment = SubscriptionPayment.objects.create(
                user=self.user, plan=self.single, selected_store=store, plan_name=self.single.name,
                amount=self.single.price, currency="MAD", duration_days=30,
                entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
                grants_premium_designs=True, provider_token=f"tok_store_{index}",
            )
            handle_subscription_webhook(payment, self.payload(payment))
        self.assertEqual(UserSubscription.objects.filter(user=self.user, status=UserSubscription.Status.ACTIVE).count(), 2)
        self.assertEqual(StoreConfig.objects.filter(store__in=[self.store_a, self.store_b], plan=StoreConfig.Plans.PREMIUM).count(), 2)

    def test_tampered_webhook_is_rejected(self):
        payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.premium, plan_name="Premium", amount="99.00", currency="MAD",
            duration_days=30, entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True, provider_token="tok_expected",
        )
        payload = self.payload(payment, amount=1, token="tok_wrong")
        raw = json.dumps(payload).encode()
        signature = hmac.new(b"private_test_key", raw, hashlib.sha256).hexdigest()
        response = APIClient().post(
            reverse("subscription-webhook"), data=raw, content_type="application/json",
            HTTP_X_YOUCANPAY_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(UserSubscription.objects.filter(user=self.user).exists())

    def test_invalid_signature_is_rejected(self):
        response = APIClient().post(
            reverse("subscription-webhook"), {"event_name": "transaction.paid"},
            format="json", HTTP_X_YOUCANPAY_SIGNATURE="invalid",
        )
        self.assertEqual(response.status_code, 401)

    def test_expiration_downgrades_store(self):
        subscription = UserSubscription.objects.create(
            user=self.user, plan=self.premium, status=UserSubscription.Status.ACTIVE,
            started_at=timezone.now() - timedelta(days=31),
            current_period_end=timezone.now() - timedelta(seconds=1),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            grants_premium_designs=True,
        )
        StoreConfig.objects.filter(store=self.store_a).update(plan=StoreConfig.Plans.PREMIUM)
        refresh_expired_subscription(subscription)
        self.store_a.config.refresh_from_db()
        self.assertEqual(subscription.status, UserSubscription.Status.EXPIRED)
        self.assertEqual(self.store_a.config.plan, StoreConfig.Plans.FREE)

    def test_expiration_refresh_rechecks_fresh_locked_state(self):
        subscription = UserSubscription.objects.create(
            user=self.user, plan=self.premium, status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() - timedelta(seconds=1),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        stale_copy = UserSubscription.objects.get(pk=subscription.pk)
        future_end = timezone.now() + timedelta(days=30)
        UserSubscription.objects.filter(pk=subscription.pk).update(current_period_end=future_end)

        refreshed = refresh_expired_subscription(stale_copy)

        self.assertEqual(refreshed.status, UserSubscription.Status.ACTIVE)
        self.assertEqual(refreshed.current_period_end, future_end)

    def test_expiration_lock_query_does_not_join_nullable_relations(self):
        subscription = UserSubscription.objects.create(
            user=self.user,
            plan=self.premium,
            selected_store=None,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )

        with CaptureQueriesContext(connection) as captured:
            refresh_expired_subscription(subscription)

        subscription_reads = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
            and "subscriptions_usersubscription" in query["sql"]
        ]
        self.assertTrue(subscription_reads)
        self.assertNotIn(" JOIN ", subscription_reads[0].upper())

    def test_store_specific_choice_entitlement_deterministically_overrides_account(self):
        choice = EntitlementDefinition.objects.create(
            key="support.service_level", name="Service level", value_type="choice",
            scope="store", allowed_values=["standard", "priority"], default_value="standard",
        )
        UserSubscription.objects.create(
            user=self.user, plan=self.premium, status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=60),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={choice.key: "standard"},
        )
        UserSubscription.objects.create(
            user=self.user, plan=self.single, selected_store=self.store_a,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=30),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
            entitlements_snapshot={choice.key: "priority"},
        )

        self.assertEqual(resolve_entitlement(self.user, choice.key, self.store_a), "priority")

    def test_checkout_accepts_idempotency_header_and_rejects_mismatch(self):
        checkout_key = str(uuid.uuid4())
        response = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.premium.id},
            format="json",
            HTTP_IDEMPOTENCY_KEY=checkout_key,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            str(SubscriptionPayment.objects.get(pk=response.data["payment_id"]).idempotency_key),
            checkout_key,
        )

        mismatch = self.authenticated_client().post(
            reverse("subscription-checkout"),
            {"plan_id": self.single.id, "store_id": self.store_a.id, "idempotency_key": str(uuid.uuid4())},
            format="json",
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(mismatch.status_code, 400)

    def test_dashboard_preflight_allows_checkout_idempotency_header(self):
        response = self.client.options(
            reverse("subscription-checkout"),
            HTTP_ORIGIN=settings.MERCHANT_DASHBOARD_URL,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type, idempotency-key, x-csrftoken",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["access-control-allow-origin"], settings.MERCHANT_DASHBOARD_URL)
        allowed = {
            header.strip().lower()
            for header in response["access-control-allow-headers"].split(",")
        }
        self.assertIn("idempotency-key", allowed)
        self.assertIn("x-csrftoken", allowed)
