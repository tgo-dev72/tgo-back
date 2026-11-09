"""PayPal Orders adapter for platform-owned SaaS billing."""

import threading
import time
from decimal import Decimal
from typing import Mapping
from urllib.parse import urlparse

import requests
from django.conf import settings

from .base import (
    CheckoutRequest,
    CheckoutSession,
    GatewayConfigurationError,
    GatewayEvent,
    GatewayPaymentError,
    GatewayVerificationUnavailable,
    InvalidGatewayEvent,
    PaymentGateway,
)


class PayPalGateway(PaymentGateway):
    code = "paypal"
    display_name = "PayPal"
    _token_cache = {}
    _token_lock = threading.Lock()

    def platform_credentials(self):
        return {
            "sandbox": True,
            "sandbox_client_id": settings.PAYPAL_CLIENT_ID,
            "sandbox_client_secret": settings.PAYPAL_CLIENT_SECRET,
            "sandbox_webhook_id": settings.PAYPAL_WEBHOOK_ID,
            "sandbox_merchant_id": settings.PAYPAL_MERCHANT_ID,
            "sandbox_receiver_email": settings.PAYPAL_RECEIVER_EMAIL,
            "live_client_id": settings.PAYPAL_LIVE_CLIENT_ID,
            "live_client_secret": settings.PAYPAL_LIVE_CLIENT_SECRET,
            "live_webhook_id": settings.PAYPAL_LIVE_WEBHOOK_ID,
            "live_merchant_id": settings.PAYPAL_LIVE_MERCHANT_ID,
            "live_receiver_email": settings.PAYPAL_LIVE_RECEIVER_EMAIL,
            "return_url": settings.PAYPAL_RETURN_URL,
            "cancel_url": settings.PAYPAL_CANCEL_URL,
            "api_timeout": settings.PAYPAL_API_TIMEOUT,
        }

    @staticmethod
    def _active_credentials(credentials):
        sandbox = bool(credentials.get("sandbox", True))
        prefix = "sandbox" if sandbox else "live"
        return {
            **credentials,
            "sandbox": sandbox,
            "client_id": credentials.get(f"{prefix}_client_id", ""),
            "client_secret": credentials.get(f"{prefix}_client_secret", ""),
            "webhook_id": credentials.get(f"{prefix}_webhook_id", ""),
            "merchant_id": credentials.get(f"{prefix}_merchant_id", ""),
            "receiver_email": credentials.get(f"{prefix}_receiver_email", ""),
            "base_url": "https://api-m.sandbox.paypal.com" if sandbox else "https://api-m.paypal.com",
        }

    def configuration_status(self, credentials):
        active = self._active_credentials(credentials)
        configured = bool(
            active.get("client_id")
            and active.get("client_secret")
            and (active.get("merchant_id") or active.get("receiver_email"))
        )
        return {
            "configured": configured,
            "sandbox": active["sandbox"],
            "checkout_credentials_configured": configured,
            "transaction_verification_configured": configured,
            "transaction_verification_required": True,
            "webhook_configured": bool(active.get("webhook_id")),
        }

    def validate_checkout(self, request, credentials):
        credentials = self._active_credentials(credentials)
        if not request.provider_hosted:
            raise GatewayConfigurationError("PayPal checkout must be provider hosted.")
        if not credentials.get("client_id") or not credentials.get("client_secret"):
            raise GatewayConfigurationError("PayPal credentials are not configured for the selected environment.")
        if not credentials.get("merchant_id") and not credentials.get("receiver_email"):
            raise GatewayConfigurationError("The PayPal receiving account identity is not configured.")
        if request.amount <= 0:
            raise GatewayConfigurationError("The purchase amount must be positive.")

    def _access_token(self, credentials):
        credentials = self._active_credentials(credentials)
        base_url = credentials["base_url"]
        if not credentials.get("client_id") or not credentials.get("client_secret"):
            raise GatewayConfigurationError("PayPal credentials are not configured for the selected environment.")
        now = time.monotonic()
        cache_key = (base_url, credentials["client_id"])
        cached = self._token_cache.get(cache_key)
        if cached and now < cached[1]:
            return cached[0]
        with self._token_lock:
            now = time.monotonic()
            cached = self._token_cache.get(cache_key)
            if cached and now < cached[1]:
                return cached[0]
            try:
                response = requests.post(
                    f"{base_url}/v1/oauth2/token",
                    auth=(credentials["client_id"], credentials["client_secret"]),
                    headers={"Accept": "application/json"},
                    data={"grant_type": "client_credentials"},
                    timeout=int(credentials.get("api_timeout", 10)),
                )
            except requests.RequestException as exc:
                raise GatewayVerificationUnavailable("PayPal authentication is unavailable.") from exc
            if response.status_code != 200:
                raise GatewayConfigurationError("PayPal authentication failed for the selected environment.")
            try:
                payload = response.json()
                token = str(payload["access_token"]).strip()
                expires_in = max(1, int(payload.get("expires_in", 300)))
            except (ValueError, KeyError, TypeError):
                raise GatewayConfigurationError("PayPal returned an invalid authentication response.")
            if not token:
                raise GatewayConfigurationError("PayPal returned an invalid authentication response.")
            self._token_cache[cache_key] = (token, time.monotonic() + max(1, expires_in - 60))
            return token

    def _request(self, method, path, credentials, *, json=None, request_id=""):
        credentials = self._active_credentials(credentials)
        base_url = credentials["base_url"]
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._access_token(credentials)}",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            response = requests.request(
                method,
                f"{base_url}{path}",
                headers=headers,
                json=json,
                timeout=int(credentials.get("api_timeout", 10)),
            )
        except requests.Timeout as exc:
            raise GatewayPaymentError("PayPal timed out. Please try again.", "provider_timeout") from exc
        except requests.RequestException as exc:
            raise GatewayPaymentError("PayPal is temporarily unavailable.", "provider_unavailable") from exc
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        return response, payload

    def create_checkout(self, request, credentials):
        self.validate_checkout(request, credentials)
        return_url = credentials.get("return_url") or request.success_url
        cancel_url = credentials.get("cancel_url") or request.error_url
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": request.order_id,
                "custom_id": request.order_id,
                "invoice_id": request.order_id,
                "description": "TijaraGO SaaS subscription",
                "amount": {
                    "currency_code": request.currency,
                    "value": f"{request.amount:.2f}",
                },
            }],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                        "user_action": "PAY_NOW",
                        "landing_page": "GUEST_CHECKOUT",
                    },
                },
            },
        }
        response, payload = self._request(
            "POST", "/v2/checkout/orders", credentials, json=body,
            request_id=f"checkout-{request.order_id}",
        )
        if response.status_code not in {200, 201}:
            raise GatewayPaymentError("PayPal could not prepare the checkout.", "order_create_failed")
        order_id = str(payload.get("id", "")).strip()
        approval_url = next((
            str(link.get("href", "")).strip()
            for link in payload.get("links", [])
            if isinstance(link, Mapping) and link.get("rel") in {"approve", "payer-action"}
        ), "")
        approval_host = (urlparse(approval_url).hostname or "").lower()
        allowed_hosts = ({"www.sandbox.paypal.com", "sandbox.paypal.com"}
                         if credentials.get("sandbox", True) else {"www.paypal.com", "paypal.com"})
        if not order_id or approval_host not in allowed_hosts:
            raise GatewayPaymentError("PayPal returned an invalid checkout response.", "malformed_response")
        return CheckoutSession(provider_token=order_id, payment_url=approval_url)

    def resume_checkout_url(self, provider_token, persisted_url, language, credentials):
        credentials = self._active_credentials(credentials)
        allowed_hosts = ({"www.sandbox.paypal.com", "sandbox.paypal.com"}
                         if credentials["sandbox"] else {"www.paypal.com", "paypal.com"})
        if not provider_token or (urlparse(persisted_url).hostname or "").lower() not in allowed_hosts:
            raise GatewayConfigurationError("This PayPal checkout cannot be resumed.")
        return persisted_url

    @staticmethod
    def _purchase_unit(payload):
        units = payload.get("purchase_units") or []
        if not units or not isinstance(units[0], Mapping):
            raise InvalidGatewayEvent("PayPal order has no purchase unit.")
        return units[0]

    def _event_from_order(self, payload, event_id, event_name, sandbox):
        unit = self._purchase_unit(payload)
        captures = ((unit.get("payments") or {}).get("captures") or [])
        capture = captures[0] if captures and isinstance(captures[0], Mapping) else {}
        status = str(capture.get("status") or payload.get("status") or "")
        amount_data = capture.get("amount") or unit.get("amount") or {}
        try:
            amount = Decimal(str(amount_data["value"]))
        except (KeyError, TypeError, ValueError):
            amount = None
        order_id = str(payload.get("id", "")).strip()
        custom_id = str(unit.get("custom_id") or unit.get("invoice_id") or unit.get("reference_id") or "").strip()
        return GatewayEvent(
            provider=self.code,
            event_id=event_id,
            event_name=event_name,
            outcome="paid" if status == "COMPLETED" and capture else ("failed" if status in {"DENIED", "DECLINED", "FAILED"} else "ignored"),
            order_id=custom_id,
            provider_transaction_id=str(capture.get("id", "")).strip(),
            provider_token=order_id,
            amount=amount,
            currency=str(amount_data.get("currency_code", "")).strip(),
            sandbox=sandbox,
            metadata={"payment_id": custom_id, "purpose": "platform_subscription"},
            raw_payload=dict(payload),
        )

    def capture_payment(self, provider_token, subject, credentials, idempotency_key):
        response, payload = self._request(
            "POST", f"/v2/checkout/orders/{provider_token}/capture", credentials,
            json={}, request_id=f"capture-{idempotency_key}",
        )
        if response.status_code == 404:
            raise GatewayPaymentError("PayPal order was not found.", "order_not_found")
        if response.status_code == 422:
            issue = str(((payload.get("details") or [{}])[0]).get("issue", ""))
            code = "approval_required" if issue in {"PAYER_ACTION_REQUIRED", "ORDER_NOT_APPROVED"} else "order_already_captured" if issue == "ORDER_ALREADY_CAPTURED" else "capture_failed"
            raise GatewayPaymentError("The PayPal order cannot be captured.", code)
        if response.status_code not in {200, 201}:
            raise GatewayPaymentError("PayPal could not capture the order.", "capture_failed")
        event = self._event_from_order(payload, f"capture:{idempotency_key}", "PAYMENT.CAPTURE.COMPLETED", bool(credentials.get("sandbox", True)))
        if event.provider_token != provider_token:
            raise InvalidGatewayEvent("PayPal returned a different order.")
        return event

    def verify_transaction(self, event, subject, credentials):
        credentials = self._active_credentials(credentials)
        response, payload = self._request("GET", f"/v2/checkout/orders/{subject.provider_token}", credentials)
        if response.status_code != 200:
            raise GatewayVerificationUnavailable("PayPal order verification is unavailable.")
        verified = self._event_from_order(payload, event.event_id, event.event_name, bool(credentials.get("sandbox", True)))
        if verified.outcome != "paid":
            raise InvalidGatewayEvent("PayPal capture is not completed.")
        checks = (
            verified.provider_token == subject.provider_token,
            verified.order_id == subject.order_id,
            verified.provider_transaction_id == event.provider_transaction_id,
            verified.amount == subject.amount,
            verified.currency == subject.currency,
        )
        if not all(checks):
            raise InvalidGatewayEvent("PayPal transaction does not match the purchase.")
        unit = self._purchase_unit(payload)
        payee = unit.get("payee") or {}
        expected_merchant = str(credentials.get("merchant_id", "")).strip()
        expected_email = str(credentials.get("receiver_email", "")).strip().lower()
        if expected_merchant and str(payee.get("merchant_id", "")).strip() != expected_merchant:
            raise InvalidGatewayEvent("PayPal receiving merchant does not match.")
        if expected_email and str(payee.get("email_address", "")).strip().lower() != expected_email:
            raise InvalidGatewayEvent("PayPal receiving account does not match.")
        return payload

    def verify_webhook(self, raw_body, headers, credentials):
        credentials = self._active_credentials(credentials)
        webhook_id = str(credentials.get("webhook_id", "")).strip()
        required = {
            "auth_algo": headers.get("PAYPAL-AUTH-ALGO"),
            "cert_url": headers.get("PAYPAL-CERT-URL"),
            "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
        }
        if not webhook_id or not all(required.values()):
            return False
        try:
            import json
            event = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        response, payload = self._request(
            "POST", "/v1/notifications/verify-webhook-signature", credentials,
            json={**required, "webhook_id": webhook_id, "webhook_event": event},
        )
        return response.status_code == 200 and payload.get("verification_status") == "SUCCESS"

    def parse_webhook(self, payload):
        if not isinstance(payload, Mapping):
            raise InvalidGatewayEvent("Webhook payload must be an object.")
        event_id = str(payload.get("id", "")).strip()
        event_name = str(payload.get("event_type", "")).strip()
        resource = payload.get("resource") or {}
        if not event_id or not event_name or not isinstance(resource, Mapping):
            raise InvalidGatewayEvent("Invalid PayPal webhook structure.")
        if event_name == "CHECKOUT.ORDER.APPROVED":
            unit = self._purchase_unit(resource)
            amount_data = unit.get("amount") or {}
            custom_id = str(unit.get("custom_id") or unit.get("invoice_id") or unit.get("reference_id") or "").strip()
            try:
                amount = Decimal(str(amount_data["value"]))
            except (KeyError, TypeError, ValueError):
                amount = None
            return GatewayEvent(self.code, event_id, event_name, "ignored", custom_id, "", str(resource.get("id", "")), amount, str(amount_data.get("currency_code", "")), None, {"payment_id": custom_id}, dict(payload))
        if event_name.startswith("PAYMENT.CAPTURE."):
            supplementary = (resource.get("supplementary_data") or {}).get("related_ids") or {}
            order_id = str(supplementary.get("order_id", "")).strip()
            amount_data = resource.get("amount") or {}
            custom_id = str(resource.get("custom_id") or resource.get("invoice_id") or "").strip()
            try:
                amount = Decimal(str(amount_data["value"]))
            except (KeyError, TypeError, ValueError):
                amount = None
            outcome = "paid" if event_name == "PAYMENT.CAPTURE.COMPLETED" and resource.get("status") == "COMPLETED" else "failed" if event_name == "PAYMENT.CAPTURE.DENIED" else "ignored"
            return GatewayEvent(self.code, event_id, event_name, outcome, custom_id, str(resource.get("id", "")), order_id, amount, str(amount_data.get("currency_code", "")), None, {"payment_id": custom_id, "purpose": "platform_subscription"}, dict(payload))
        return GatewayEvent(self.code, event_id, event_name, "ignored", "", "", "", None, "", None, {}, dict(payload))
