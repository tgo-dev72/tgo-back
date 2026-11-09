"""Contract tests proving billing services are independent of provider shape."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from catalog.models import CatalogSection, Product, StockVariant
from orders.models import Order
from orders.services import create_order
from payments.gateways.base import CheckoutSession, GatewayEvent, PaymentGateway
from payments.models import PaymentTransaction
from store.models import Store, StoreConfig, StoreMember
from subscriptions.models import SubscriptionPayment, SubscriptionPlan, UserSubscription
from subscriptions.services import create_subscription_checkout


class FakeGateway(PaymentGateway):
    """A deliberately non-YouCanPay payload used as an adapter contract test."""

    code = "fakepay"
    display_name = "Fake Pay"

    def platform_credentials(self):
        return {"secret": "platform-secret", "sandbox": True}

    def configuration_status(self, credentials):
        return {"configured": bool(credentials.get("secret")), "sandbox": bool(credentials.get("sandbox"))}

    def validate_checkout(self, request, credentials):
        if not credentials.get("secret"):
            raise RuntimeError("missing fake credentials")

    def create_checkout(self, request, credentials):
        self.validate_checkout(request, credentials)
        token = f"fake-token-{request.order_id}"
        return CheckoutSession(token, f"https://fake-pay.test/checkout/{token}")

    def verify_webhook(self, raw_body, headers, credentials):
        return headers.get("X-Fake-Signature") == credentials.get("secret")

    def parse_webhook(self, payload):
        return GatewayEvent(
            provider=self.code,
            event_id=str(payload.get("event", "")),
            event_name=str(payload.get("kind", "")),
            outcome=str(payload.get("result", "ignored")),
            order_id=str(payload.get("merchant_reference", "")),
            provider_transaction_id=str(payload.get("charge", "")),
            provider_token=str(payload.get("session", "")),
            amount=Decimal(str(payload["total"])) if payload.get("total") is not None else None,
            currency=str(payload.get("currency", "")),
            sandbox=payload.get("test"),
            metadata=payload.get("custom") or {},
            raw_payload=payload,
        )

    def credential_fields(self):
        return [{"key": "secret", "label": "Secret", "secret": True, "required": True}]


class SecondFakeGateway(FakeGateway):
    code = "secondpay"
    display_name = "Second Pay"

    def platform_credentials(self):
        return {"secret": "second-platform-secret", "sandbox": True}


GATEWAY_SETTINGS = {
    "PAYMENT_GATEWAY_BACKENDS": {"fakepay": "payments.tests_gateways.FakeGateway"},
    "PLATFORM_PAYMENT_GATEWAY": "fakepay",
    "STOREFRONT_DEFAULT_PAYMENT_GATEWAY": "fakepay",
    "MERCHANT_DASHBOARD_URL": "https://dash.example.test",
}


@override_settings(**GATEWAY_SETTINGS)
class GatewayIndependenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        cls.user = user_model.objects.create_user(email="gateway@example.com", password="pass")
        MerchantProfile.objects.create(user=cls.user, business_name="Gateway shop")
        cls.store = Store.objects.create(name="Gateway shop", slug="gateway-shop")
        StoreMember.objects.create(user=cls.user, store=cls.store, role=StoreMember.Roles.OWNER)
        StoreConfig.objects.filter(store=cls.store).update(
            payment_provider="fakepay",
            payment_keys={"secret": "store-secret", "sandbox": True},
        )
        section = CatalogSection.objects.create(store=cls.store, key="main", label="Main")
        product = Product.objects.create(store=cls.store, section=section, ref="item", name="Item", price="20.00")
        cls.variant = StockVariant.objects.create(store=cls.store, product=product, attributes={}, quantity=5)
        cls.product = product
        cls.plan = SubscriptionPlan.objects.get(slug="premium")
        cls.plan.price = Decimal("99.00")
        cls.plan.is_active = True
        cls.plan.save()

    def test_storefront_checkout_and_webhook_use_only_gateway_contract(self):
        order = create_order(
            self.store,
            customer={"name": "Customer"},
            items=[{"product_id": self.product.pk, "variant_id": self.variant.pk, "quantity": 1}],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        client = APIClient()
        checkout = client.post(
            reverse("payment-create"),
            {"order_id": str(order.order_id), "success_url": "https://shop.test/success", "error_url": "https://shop.test/error"},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(checkout.status_code, 200, checkout.data)
        payment = PaymentTransaction.objects.get(order=order)
        self.assertEqual(payment.provider, "fakepay")
        self.assertIn("fake-pay.test", checkout.data["payment_url"])

        event = {
            "event": "store-event-1",
            "kind": "charge.completed",
            "result": "paid",
            "merchant_reference": str(order.order_id),
            "charge": "fake-charge-store",
            "session": payment.provider_token,
            "total": str(order.amount),
            "currency": order.currency,
            "test": True,
        }
        response = client.post(
            reverse("payment-provider-webhook", kwargs={"provider": "fakepay"}),
            event,
            format="json",
            HTTP_X_FAKE_SIGNATURE="store-secret",
        )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.is_paid, Order.IsPaid.CONFIRMED)

    def test_environment_default_is_applied_to_new_stores(self):
        store = Store.objects.create(name="Default gateway", slug="default-gateway")
        self.assertEqual(store.config.payment_provider, "fakepay")

    def test_membership_checkout_and_webhook_use_same_contract_with_separate_credentials(self):
        payment, url = create_subscription_checkout(self.user, self.plan)
        self.assertEqual(payment.provider, "fakepay")
        self.assertEqual(payment.checkout_url, url)
        self.assertIn("fake-pay.test", url)
        event = {
            "event": "membership-event-1",
            "kind": "charge.completed",
            "result": "paid",
            "merchant_reference": str(payment.pk),
            "charge": "fake-charge-membership",
            "session": payment.provider_token,
            "total": str(payment.amount),
            "currency": payment.currency,
            "test": True,
            "custom": {"purpose": "platform_subscription", "payment_id": str(payment.pk)},
        }
        response = APIClient().post(
            reverse("subscription-provider-webhook", kwargs={"provider": "fakepay"}),
            event,
            format="json",
            HTTP_X_FAKE_SIGNATURE="platform-secret",
        )
        self.assertEqual(response.status_code, 200, response.data)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        self.assertTrue(UserSubscription.objects.filter(user=self.user, status="active").exists())

    def test_provider_specific_webhook_rejects_wrong_credential_domain(self):
        payment, _url = create_subscription_checkout(self.user, self.plan)
        response = APIClient().post(
            reverse("subscription-provider-webhook", kwargs={"provider": "fakepay"}),
            {"merchant_reference": str(payment.pk)},
            format="json",
            HTTP_X_FAKE_SIGNATURE="store-secret",
        )
        self.assertEqual(response.status_code, 401)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.PENDING)

    def test_legacy_webhook_still_routes_pending_payment_after_default_gateway_switch(self):
        payment, _url = create_subscription_checkout(self.user, self.plan)
        event = {
            "event": "old-provider-after-switch",
            "kind": "charge.completed",
            "result": "paid",
            "merchant_reference": str(payment.pk),
            "charge": "old-provider-charge",
            "session": payment.provider_token,
            "total": str(payment.amount),
            "currency": payment.currency,
            "test": True,
            "custom": {"purpose": "platform_subscription", "payment_id": str(payment.pk)},
        }
        switched = {
            "PAYMENT_GATEWAY_BACKENDS": {
                "secondpay": "payments.tests_gateways.SecondFakeGateway",
                "fakepay": "payments.tests_gateways.FakeGateway",
            },
            "PLATFORM_PAYMENT_GATEWAY": "secondpay",
        }
        with override_settings(**switched):
            response = APIClient().post(
                reverse("subscription-webhook"),
                event,
                format="json",
                HTTP_X_FAKE_SIGNATURE="platform-secret",
            )
        self.assertEqual(response.status_code, 200, response.data)
        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)

    def test_storefront_provider_switch_preserves_pending_webhook_credentials(self):
        order = create_order(
            self.store,
            customer={"name": "Customer"},
            items=[{"product_id": self.product.pk, "variant_id": self.variant.pk, "quantity": 1}],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        checkout = APIClient().post(
            reverse("payment-create"),
            {"order_id": str(order.order_id), "success_url": "https://shop.test/success", "error_url": "https://shop.test/error"},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(checkout.status_code, 200)
        payment = PaymentTransaction.objects.get(order=order)
        StoreConfig.objects.filter(store=self.store).update(
            payment_provider="secondpay",
            payment_keys={
                "fakepay": {"secret": "store-secret", "sandbox": True},
                "secondpay": {"secret": "second-store-secret", "sandbox": True},
            },
        )
        event = {
            "event": "old-store-provider-after-switch",
            "kind": "charge.completed",
            "result": "paid",
            "merchant_reference": str(order.order_id),
            "charge": "old-store-provider-charge",
            "session": payment.provider_token,
            "total": str(order.amount),
            "currency": order.currency,
            "test": True,
        }
        switched = {
            "PAYMENT_GATEWAY_BACKENDS": {
                "secondpay": "payments.tests_gateways.SecondFakeGateway",
                "fakepay": "payments.tests_gateways.FakeGateway",
            },
            "STOREFRONT_DEFAULT_PAYMENT_GATEWAY": "secondpay",
        }
        with override_settings(**switched):
            response = APIClient().post(
                reverse("payment-webhook"),
                event,
                format="json",
                HTTP_X_FAKE_SIGNATURE="store-secret",
            )
        self.assertEqual(response.status_code, 200, response.data)
        order.refresh_from_db()
        self.assertEqual(order.is_paid, Order.IsPaid.CONFIRMED)
