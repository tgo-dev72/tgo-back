"""End-to-end tests for the tenant-scoped payments app."""
from unittest.mock import patch
import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from catalog.models import AttributeDef, CatalogSection, Product, StockVariant
from orders.models import Order
from orders.services import create_order
from payments.models import PaymentTransaction
from payments.gateways.base import CheckoutRequest, GatewayConfigurationError
from payments.gateways.youcanpay import YouCanPayGateway
from payments.services import (
    PaymentError,
    PaymentConfigError,
    cancel_payment,
    create_payment_url,
    handle_webhook,
    retry_payment_url,
    verify_status,
)
from store.models import Store, StoreConfig


def make_store(slug="pay", keys=True):
    store, _ = Store.objects.get_or_create(slug=slug, defaults={"name": "Pay Store"})
    if keys:
        StoreConfig.objects.update_or_create(
            store=store,
            defaults={"payment_keys": {"private_key": "priv", "public_key": "pub", "sandbox": True}},
        )
    section, _ = CatalogSection.objects.get_or_create(
        store=store, key="soap", defaults={"label": "Soap"}
    )
    product = Product.objects.create(store=store, section=section, ref="soap-1", name="Soap", price=100)
    variant = StockVariant.objects.create(store=store, product=product, attributes={}, quantity=10)
    return store, product, variant


class YouCanPayExternalCheckoutTests(SimpleTestCase):
    def checkout_request(self):
        return CheckoutRequest(
            order_id="87696ba2-4191-4da7-81d7-08e66714abbb",
            amount=Decimal("149.00"),
            currency="MAD",
            success_url="https://dash.example.test/subscription?payment=success",
            error_url="https://dash.example.test/subscription?payment=failed",
            provider_hosted=True,
        )

    def credentials(self):
        return {
            "private_key": "private_test_key",
            "public_key": "public_test_key",
            "access_token": "",
            "sandbox": True,
            "api_timeout": 10,
        }

    @patch("requests.post")
    def test_sandbox_checkout_tokenizes_and_returns_external_payment_form(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {
            "transaction_id": "transaction_123",
            "token": {"id": "sandbox_token_123"},
        }

        session = YouCanPayGateway().create_checkout(self.checkout_request(), self.credentials())

        self.assertEqual(session.provider_token, "sandbox_token_123")
        self.assertEqual(
            session.payment_url,
            "https://youcanpay.com/sandbox/payment-form/sandbox_token_123?lang=en",
        )
        request_kwargs = provider_post.call_args.kwargs
        self.assertEqual(provider_post.call_args.args[0], "https://youcanpay.com/sandbox/api/tokenize")
        self.assertEqual(request_kwargs["data"]["order_id"], self.checkout_request().order_id)
        self.assertEqual(request_kwargs["data"]["amount"], "14900")
        self.assertEqual(request_kwargs["data"]["pri_key"], "private_test_key")
        self.assertNotIn("Authorization", request_kwargs["headers"])

    @patch("requests.post")
    def test_invalid_provider_token_is_rejected(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"token": "../collect-card"}

        with self.assertRaises(GatewayConfigurationError):
            YouCanPayGateway().create_checkout(self.checkout_request(), self.credentials())

    def test_live_checkout_url_omits_sandbox_path(self):
        credentials = {**self.credentials(), "sandbox": False}
        self.assertEqual(
            YouCanPayGateway().checkout_url("live_token_123", "fr", credentials),
            "https://youcanpay.com/payment-form/live_token_123?lang=fr",
        )

    def test_checkout_does_not_require_oauth_unless_readback_is_mandatory(self):
        gateway = YouCanPayGateway()
        credentials = {**self.credentials(), "access_token": ""}
        self.assertTrue(gateway.configuration_status(credentials)["configured"])
        credentials["require_transaction_verification"] = True
        self.assertFalse(gateway.configuration_status(credentials)["configured"])

    def test_resume_rebuilds_sandbox_url_from_the_persisted_token(self):
        gateway = YouCanPayGateway()
        url = gateway.resume_checkout_url(
            "safe_token",
            "https://platform.example/api/subscriptions/checkout/id/pay?state=secret",
            "fr",
            self.credentials(),
        )
        self.assertEqual(url, "https://youcanpay.com/sandbox/payment-form/safe_token?lang=fr")

    def test_token_webhook_is_normalized(self):
        event = YouCanPayGateway().parse_webhook({
            "id": "evt_token",
            "event_name": "transaction.paid",
            "sandbox": True,
            "payload": {
                "token": {"id": "sandbox_token"},
                "metadata": {"purpose": "platform_subscription"},
                "transaction": {
                    "id": "txn_token",
                    "order_id": "payment_123",
                    "amount": 14900,
                    "currency": "MAD",
                    "status": 1,
                },
            },
        })
        self.assertEqual(event.order_id, "payment_123")
        self.assertEqual(event.provider_token, "sandbox_token")
        self.assertEqual(event.amount, Decimal("149.00"))


class PaymentServiceTests(TestCase):
    def setUp(self):
        self.store, self.product, self.variant = make_store()
        self.order = create_order(
            self.store,
            customer={"name": "Amina", "email": "a@b.c", "phone": "0600000000"},
            items=[{"product_id": self.product.id, "variant_id": self.variant.id, "quantity": 2}],
            payment_mode=Order.PaymentMode.ONLINE,
        )

    def webhook_payload(self, event_name="transaction.paid", status=1, token="tok_123", transaction="ycp_1"):
        PaymentTransaction.objects.create(
            store=self.store,
            order=self.order,
            provider_token=token,
            amount=self.order.amount,
            currency=self.order.currency,
        )
        return {
            "event_name": event_name,
            "payload": {
                "token": {"id": token},
                "transaction": {
                    "id": transaction,
                    "order_id": str(self.order.order_id),
                    "status": status,
                    "amount": str(int(self.order.amount * 100)),
                    "currency": self.order.currency,
                },
            },
        }

    @patch("requests.post")
    def test_storefront_online_checkout_uses_the_hosted_provider_flow(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"token": {"id": "storefront_token"}}
        url, payment = create_payment_url(self.store, self.order, "https://s?order_id=old&lang=fr", "https://e?order_id=old#checkout", "fr")
        self.assertEqual(url, "https://youcanpay.com/sandbox/payment-form/storefront_token?lang=fr")
        data = provider_post.call_args.kwargs["data"]
        self.assertEqual(parse_qs(urlsplit(data["success_url"]).query), {"order_id": [str(self.order.order_id)], "lang": ["fr"]})
        self.assertEqual(parse_qs(urlsplit(data["error_url"]).query), {"order_id": [str(self.order.order_id)]})
        self.assertEqual(urlsplit(data["error_url"]).fragment, "checkout")
        self.assertEqual(payment.status, PaymentTransaction.Status.PENDING)
        # Opening the provider page does not collect payment or consume stock.
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.PENDING)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 2)
        self.assertEqual(self.variant.quantity, 10)

    @patch("requests.post")
    def test_storefront_token_is_never_turned_into_an_obsolete_hosted_url(self, provider_post):
        self.store.config.payment_keys = {
            "private_key": "priv_live",
            "public_key": "pub_live",
            "sandbox": False,
        }
        self.store.config.save(update_fields=["payment_keys"])

        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"token": "live_storefront_token"}
        url, _ = create_payment_url(self.store, self.order, "https://s", "https://e", "en")
        self.assertEqual(url, "https://youcanpay.com/payment-form/live_storefront_token?lang=en")

    def test_missing_keys_raises_config_error(self):
        store, _, _ = make_store(slug="paynokeys", keys=False)
        with self.assertRaises(PaymentConfigError):
            create_payment_url(store, self.order, "https://s", "https://e")

    def test_webhook_paid_marks_payment_but_keeps_fulfillment_pending(self):
        handle_webhook(self.store, self.order, self.webhook_payload())
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.CONFIRMED)
        self.assertEqual(self.order.transaction_id, "ycp_1")
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 10)
        self.assertEqual(self.variant.reserved, 2)
        pt = PaymentTransaction.objects.get(order=self.order)
        self.assertEqual(pt.status, PaymentTransaction.Status.CONFIRMED)

    def test_webhook_failure_releases_reservations(self):
        handle_webhook(self.store, self.order, self.webhook_payload("transaction.failed", 0, transaction="ycp_2"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 10)
        self.assertEqual(self.variant.reserved, 0)

    def test_webhook_idempotent(self):
        payload = self.webhook_payload(transaction="ycp_3")
        handle_webhook(self.store, self.order, payload)
        handle_webhook(self.store, self.order, payload)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 10)
        self.assertEqual(self.variant.reserved, 2)  # no duplicate reservation

    def test_webhook_ignored_once_treated(self):
        handle_webhook(self.store, self.order, self.webhook_payload(transaction="ycp_4"))
        # a second, contradictory webhook must not touch the confirmed order
        payload = self.webhook_payload("transaction.failed", 0, transaction="ycp_5")
        handle_webhook(self.store, self.order, payload)
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.CONFIRMED)

    @patch("requests.post")
    def test_retry_does_not_issue_an_obsolete_provider_url(self, provider_post):
        handle_webhook(self.store, self.order, self.webhook_payload("transaction.failed", 0, transaction="ycp_6"))
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
        provider_post.return_value.status_code = 503
        with self.assertRaises(PaymentError):
            retry_payment_url(self.store, self.order, "https://s", "https://e")
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)

    def test_retry_is_rejected_when_released_stock_was_sold(self):
        handle_webhook(self.store, self.order, self.webhook_payload("transaction.failed", 0, transaction="ycp_sold"))
        self.variant.refresh_from_db()
        self.variant.quantity = 0
        self.variant.save(update_fields=["quantity"])

        with self.assertRaisesRegex(PaymentError, "can no longer"):
            retry_payment_url(self.store, self.order, "https://s", "https://e")

        self.order.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
        self.assertEqual(self.variant.reserved, 0)

    def test_cancel_only_while_pending(self):
        cancel_payment(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)

    def test_cancel_after_confirm_rejected(self):
        handle_webhook(self.store, self.order, self.webhook_payload(transaction="ycp_7"))
        from payments.services import PaymentError
        with self.assertRaises(PaymentError):
            cancel_payment(self.order)

    def test_verify_status(self):
        self.assertEqual(verify_status(self.order), "pending")
        handle_webhook(self.store, self.order, self.webhook_payload(transaction="ycp_8"))
        self.order.refresh_from_db()
        self.assertEqual(verify_status(self.order), "confirmed")


class PaymentApiTests(TestCase):
    def setUp(self):
        self.store, self.product, self.variant = make_store()
        self.client = APIClient()
        self.order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[{"product_id": self.product.id, "variant_id": self.variant.id, "quantity": 1}],
            payment_mode=Order.PaymentMode.ONLINE,
        )

    def _signed_webhook(self, payload):
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(b"priv", raw, hashlib.sha256).hexdigest()
        return raw, signature

    def test_webhook_endpoint_confirms_via_order(self):
        PaymentTransaction.objects.create(
            store=self.store, order=self.order, provider_token="tok_api",
            amount=self.order.amount, currency=self.order.currency,
        )
        payload = {
            "event_name": "transaction.paid",
            "payload": {
                "token": {"id": "tok_api"},
                "transaction": {
                    "id": "ycp_api", "order_id": str(self.order.order_id), "status": 1,
                    "amount": str(int(self.order.amount * 100)), "currency": self.order.currency,
                },
            },
        }
        raw, signature = self._signed_webhook(payload)
        res = self.client.post(
            reverse("payment-webhook"),
            raw, content_type="application/json", HTTP_X_YOUCANPAY_SIGNATURE=signature,
        )
        self.assertEqual(res.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.CONFIRMED)

    def test_webhook_endpoint_rejects_invalid_signature(self):
        res = self.client.post(
            reverse("payment-webhook"),
            {"event_name": "transaction.paid", "payload": {"transaction": {"order_id": str(self.order.order_id)}}},
            format="json", HTTP_X_YOUCANPAY_SIGNATURE="not-valid",
        )
        self.assertEqual(res.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.PENDING)

    @patch("requests.post")
    def test_create_endpoint(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"token": "api_storefront_token"}
        res = self.client.post(
            reverse("payment-create"),
            {"order_id": str(self.order.order_id), "success_url": "https://s", "error_url": "https://e", "tokenParams": {"lang": "en"}},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["payment_url"], "https://youcanpay.com/sandbox/payment-form/api_storefront_token?lang=en")

    def test_create_endpoint_unknown_store_404(self):
        res = self.client.post(
            reverse("payment-create"),
            {"order_id": str(self.order.order_id), "success_url": "https://s", "error_url": "https://e"},
            format="json",
            HTTP_X_STORE_SLUG="nope",
        )
        self.assertEqual(res.status_code, 404)

    def test_verify_endpoint(self):
        res = self.client.post(
            reverse("payment-verify"),
            {"order_id": str(self.order.order_id)},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(res.data["status"], "pending")
        self.assertFalse(res.data["verified"])

    def test_cancel_endpoint(self):
        res = self.client.post(
            reverse("payment-cancel"),
            {"order_id": str(self.order.order_id)},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(res.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.is_paid, Order.IsPaid.FAILED)
