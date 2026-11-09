from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from payments.gateways.base import CheckoutRequest, PaymentSubject
from payments.gateways.paypal import PayPalGateway


SETTINGS = dict(
    PAYPAL_CLIENT_ID="sandbox-client",
    PAYPAL_CLIENT_SECRET="sandbox-secret",
    PAYPAL_WEBHOOK_ID="WH-test",
    PAYPAL_RETURN_URL="https://dash.example.test/return",
    PAYPAL_CANCEL_URL="https://dash.example.test/cancel",
    PAYPAL_MERCHANT_ID="merchant-test",
    PAYPAL_RECEIVER_EMAIL="billing@example.test",
    PAYPAL_LIVE_CLIENT_ID="live-client",
    PAYPAL_LIVE_CLIENT_SECRET="live-secret",
    PAYPAL_LIVE_WEBHOOK_ID="WH-live",
    PAYPAL_LIVE_MERCHANT_ID="merchant-live",
    PAYPAL_LIVE_RECEIVER_EMAIL="live@example.test",
    PAYPAL_API_TIMEOUT=3,
)


def response(status, payload):
    return SimpleNamespace(status_code=status, content=b"{}", json=lambda: payload)


@override_settings(**SETTINGS)
class PayPalGatewayTests(SimpleTestCase):
    def setUp(self):
        PayPalGateway._token_cache = {}
        self.gateway = PayPalGateway()
        self.credentials = self.gateway.platform_credentials()

    @patch("payments.gateways.paypal.requests.post")
    def test_oauth_token_is_cached(self, post):
        post.return_value = response(200, {"access_token": "token", "expires_in": 3600})
        self.assertEqual(self.gateway._access_token(self.credentials), "token")
        self.assertEqual(self.gateway._access_token(self.credentials), "token")
        post.assert_called_once()

    @patch.object(PayPalGateway, "_request")
    def test_create_order_uses_server_amount_and_extracts_approval_url(self, request):
        request.return_value = response(201, {}), {
            "id": "ORDER-1",
            "links": [{"rel": "approve", "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1"}],
        }
        session = self.gateway.create_checkout(CheckoutRequest(
            order_id="local-1", amount=Decimal("99.00"), currency="USD",
            success_url="https://dash.example.test/success",
            error_url="https://dash.example.test/error", provider_hosted=True,
        ), self.credentials)
        self.assertEqual(session.provider_token, "ORDER-1")
        self.assertIn("sandbox.paypal.com", session.payment_url)
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["purchase_units"][0]["amount"], {"currency_code": "USD", "value": "99.00"})
        self.assertEqual(body["purchase_units"][0]["custom_id"], "local-1")
        self.assertEqual(
            body["payment_source"]["paypal"]["experience_context"]["landing_page"],
            "GUEST_CHECKOUT",
        )
        self.assertNotIn("application_context", body)

    @patch.object(PayPalGateway, "_request")
    def test_capture_normalizes_completed_payment(self, request):
        request.return_value = response(201, {}), {
            "id": "ORDER-1", "status": "COMPLETED",
            "purchase_units": [{
                "custom_id": "local-1",
                "amount": {"currency_code": "USD", "value": "99.00"},
                "payments": {"captures": [{
                    "id": "CAPTURE-1", "status": "COMPLETED",
                    "amount": {"currency_code": "USD", "value": "99.00"},
                }]},
            }],
        }
        event = self.gateway.capture_payment(
            "ORDER-1",
            PaymentSubject("local-1", Decimal("99.00"), "USD", "ORDER-1"),
            self.credentials, "local-1",
        )
        self.assertEqual(event.outcome, "paid")
        self.assertEqual(event.provider_transaction_id, "CAPTURE-1")
        self.assertEqual(event.amount, Decimal("99.00"))

    def test_live_configuration_uses_separate_credentials(self):
        credentials = {**self.credentials, "sandbox": False}
        status = self.gateway.configuration_status(credentials)
        self.assertTrue(status["configured"])
        self.assertFalse(status["sandbox"])
        self.assertEqual(
            self.gateway._active_credentials(credentials)["base_url"],
            "https://api-m.paypal.com",
        )

    def test_order_approved_is_never_paid(self):
        event = self.gateway.parse_webhook({
            "id": "WH-1", "event_type": "CHECKOUT.ORDER.APPROVED",
            "resource": {"id": "ORDER-1", "purchase_units": [{
                "custom_id": "local-1", "amount": {"currency_code": "USD", "value": "99.00"},
            }]},
        })
        self.assertEqual(event.outcome, "ignored")
