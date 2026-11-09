from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from payments.gateways.base import GatewayEvent
from subscriptions.models import SubscriptionPayment, SubscriptionPlan, UserSubscription
from subscriptions.services import SubscriptionError, capture_subscription_payment


@override_settings(
    PLATFORM_PAYMENT_GATEWAY="paypal",
    PAYPAL_ENV="sandbox",
    PAYPAL_CLIENT_ID="sandbox-client",
    PAYPAL_CLIENT_SECRET="sandbox-secret",
    PAYPAL_WEBHOOK_ID="WH-test",
    PAYPAL_BASE_URL="https://api-m.sandbox.paypal.com",
    PAYPAL_RETURN_URL="https://dash.example.test/return",
    PAYPAL_CANCEL_URL="https://dash.example.test/cancel",
    PAYPAL_MERCHANT_ID="merchant-test",
    PAYPAL_RECEIVER_EMAIL="billing@example.test",
    PAYPAL_API_TIMEOUT=3,
)
class PayPalSubscriptionSettlementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(email="paypal@example.test", password="pass")
        self.other = User.objects.create_user(email="other-paypal@example.test", password="pass")
        MerchantProfile.objects.create(user=self.user, business_name="PayPal merchant")
        MerchantProfile.objects.create(user=self.other, business_name="Other merchant")
        self.plan = SubscriptionPlan.objects.create(
            name="PayPal test", slug="paypal-test", price=Decimal("12.00"),
            currency="USD", duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        self.payment = SubscriptionPayment.objects.create(
            user=self.user, plan=self.plan, plan_name=self.plan.name,
            gross_amount=self.plan.price, amount=self.plan.price, currency="USD",
            duration_days=30, entitlement_scope=self.plan.entitlement_scope,
            plan_revision=self.plan.revision, provider="paypal",
            provider_token="ORDER-TEST", entitlements_snapshot={},
        )

    def event(self):
        return GatewayEvent(
            provider="paypal", event_id="capture:CAPTURE-TEST",
            event_name="PAYMENT.CAPTURE.COMPLETED", outcome="paid",
            order_id=str(self.payment.pk), provider_transaction_id="CAPTURE-TEST",
            provider_token="ORDER-TEST", amount=Decimal("12.00"), currency="USD",
            sandbox=True,
            metadata={"payment_id": str(self.payment.pk), "purpose": "platform_subscription"},
            raw_payload={"id": "ORDER-TEST", "status": "COMPLETED"},
        )

    def approved_payload(self):
        return {
            "id": "WH-APPROVED-TEST",
            "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {
                "id": "ORDER-TEST",
                "status": "APPROVED",
                "purchase_units": [{
                    "custom_id": str(self.payment.pk),
                    "amount": {"currency_code": "USD", "value": "12.00"},
                }],
            },
        }

    @patch("payments.gateways.paypal.PayPalGateway.verify_transaction", return_value={})
    @patch("payments.gateways.paypal.PayPalGateway.capture_payment")
    def test_capture_activates_once_and_retry_is_idempotent(self, capture, _verify):
        capture.return_value = self.event()
        first = capture_subscription_payment(self.user, "ORDER-TEST")
        second = capture_subscription_payment(self.user, "ORDER-TEST")
        self.assertEqual(first.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(second.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(UserSubscription.objects.filter(user=self.user, status="active").count(), 1)
        capture.assert_called_once()

    @patch("payments.gateways.paypal.PayPalGateway.verify_transaction", return_value={})
    @patch("payments.gateways.paypal.PayPalGateway.capture_payment")
    def test_live_capture_uses_payment_environment_not_gateway_default(self, capture, _verify):
        self.payment.sandbox = False
        self.payment.save(update_fields=["sandbox"])
        event = self.event()
        event = event.__class__(
            event.provider, event.event_id, event.event_name, event.outcome,
            event.order_id, event.provider_transaction_id, event.provider_token,
            event.amount, event.currency, False, event.metadata, event.raw_payload,
        )
        capture.return_value = event

        result = capture_subscription_payment(self.user, "ORDER-TEST")

        self.assertEqual(result.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertTrue(UserSubscription.objects.filter(user=self.user, status="active").exists())

    def test_wrong_merchant_cannot_capture_order(self):
        with self.assertRaises(SubscriptionError):
            capture_subscription_payment(self.other, "ORDER-TEST")

    @patch("payments.gateways.paypal.PayPalGateway.verify_webhook", return_value=True)
    @patch("payments.gateways.paypal.PayPalGateway.verify_transaction", return_value={})
    @patch("payments.gateways.paypal.PayPalGateway.capture_payment")
    def test_verified_approval_webhook_captures_and_activates(
        self, capture, _verify_transaction, _verify_webhook,
    ):
        capture.return_value = self.event()

        response = APIClient().post(
            reverse("subscription-provider-webhook", kwargs={"provider": "paypal"}),
            self.approved_payload(),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertEqual(UserSubscription.objects.filter(
            user=self.user, status=UserSubscription.Status.ACTIVE,
        ).count(), 1)
        capture.assert_called_once()

    @patch("subscriptions.services._activate_payment_locked", side_effect=RuntimeError("database failure"))
    @patch("payments.gateways.paypal.PayPalGateway.verify_transaction", return_value={})
    @patch("payments.gateways.paypal.PayPalGateway.capture_payment")
    def test_activation_failure_preserves_recoverable_capture(self, capture, _verify, _activate):
        capture.return_value = self.event()
        with self.assertRaises(RuntimeError):
            capture_subscription_payment(self.user, "ORDER-TEST")
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, SubscriptionPayment.Status.CAPTURED)
        self.assertEqual(self.payment.provider_transaction_id, "CAPTURE-TEST")
        self.assertFalse(UserSubscription.objects.filter(user=self.user, status="active").exists())
