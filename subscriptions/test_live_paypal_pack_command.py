import io
from decimal import Decimal
from django.core.management import call_command
from django.test import TestCase, override_settings

from payments.models import PlatformPaymentGateway
from subscriptions.models import SubscriptionPlan


@override_settings(
    PAYPAL_LIVE_CLIENT_ID="live-client",
    PAYPAL_LIVE_CLIENT_SECRET="live-secret",
    PAYPAL_LIVE_MERCHANT_ID="live-merchant",
    PAYPAL_LIVE_RECEIVER_EMAIL="billing@example.test",
    PAYPAL_LIVE_WEBHOOK_ID="live-webhook",
)
class LivePayPalTestPackCommandTests(TestCase):
    def test_dry_run_does_not_write(self):
        call_command("configure_live_paypal_test_pack", stdout=io.StringIO())
        self.assertFalse(SubscriptionPlan.objects.filter(slug="paypal-live-test-001").exists())

    def test_apply_is_idempotent_and_enables_only_live_paypal(self):
        call_command("configure_live_paypal_test_pack", "--apply", stdout=io.StringIO())
        call_command("configure_live_paypal_test_pack", "--apply", stdout=io.StringIO())
        plan = SubscriptionPlan.objects.get(slug="paypal-live-test-001")
        self.assertEqual(str(plan.price), "0.01")
        self.assertEqual(plan.currency, "USD")
        self.assertEqual(plan.duration_days, 1)
        self.assertEqual(SubscriptionPlan.objects.filter(slug=plan.slug).count(), 1)
        paypal = PlatformPaymentGateway.objects.get(code="paypal")
        youcanpay = PlatformPaymentGateway.objects.get(code="youcanpay")
        self.assertTrue(paypal.is_enabled)
        self.assertFalse(paypal.sandbox_enabled)
        self.assertFalse(youcanpay.is_enabled)

    @override_settings(
        PAYPAL_LIVE_CLIENT_ID="",
        PAYPAL_LIVE_CLIENT_SECRET="",
        PAYPAL_LIVE_MERCHANT_ID="",
        PAYPAL_LIVE_RECEIVER_EMAIL="",
    )
    def test_plan_only_does_not_require_credentials_or_change_gateways(self):
        call_command(
            "configure_live_paypal_test_pack", "--apply", "--plan-only", stdout=io.StringIO(),
        )
        plan = SubscriptionPlan.objects.get(slug="paypal-live-test-001")
        self.assertEqual(plan.price, Decimal("0.01"))
        self.assertEqual(plan.currency, "USD")
        self.assertFalse(PlatformPaymentGateway.objects.filter(code="paypal").exists())
