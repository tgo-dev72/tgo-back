import hashlib
import hmac
from decimal import Decimal, ROUND_HALF_UP
import re
from typing import Any, Mapping
from urllib.parse import urlencode

from django.conf import settings

from .base import (
    CheckoutRequest,
    CheckoutSession,
    GatewayConfigurationError,
    GatewayEvent,
    GatewayVerificationUnavailable,
    InvalidGatewayEvent,
    PaymentGateway,
    PaymentSubject,
)


class YouCanPayGateway(PaymentGateway):
    code = "youcanpay"
    display_name = "YouCanPay"
    token_endpoint = "https://youcanpay.com/api/tokenize"
    sandbox_token_endpoint = "https://youcanpay.com/sandbox/api/tokenize"
    _TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")

    def platform_credentials(self):
        return {
            "private_key": settings.PLATFORM_PAYMENT_PRIVATE_KEY,
            "public_key": settings.PLATFORM_PAYMENT_PUBLIC_KEY,
            "access_token": settings.PLATFORM_PAYMENT_ACCESS_TOKEN,
            "sandbox": settings.PLATFORM_PAYMENT_SANDBOX,
            "require_transaction_verification": settings.PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION,
            "api_timeout": settings.PLATFORM_PAYMENT_API_TIMEOUT,
        }

    def configuration_status(self, credentials):
        checkout_ready = bool(credentials.get("private_key"))
        truth_ready = bool(credentials.get("access_token"))
        truth_required = bool(credentials.get("require_transaction_verification", False))
        return {
            "configured": checkout_ready and (truth_ready or not truth_required),
            "sandbox": bool(credentials.get("sandbox")),
            "checkout_credentials_configured": checkout_ready,
            "transaction_verification_configured": truth_ready,
            "transaction_verification_required": truth_required,
        }

    def credential_fields(self):
        return [
            {"key": "private_key", "label": "Private key", "secret": True, "required": True},
            {"key": "public_key", "label": "Public key", "secret": False, "required": True},
            {"key": "sandbox", "label": "Test mode", "type": "boolean", "secret": False, "required": False},
        ]

    def validate_checkout(self, request, credentials):
        if not request.provider_hosted:
            raise GatewayConfigurationError("Online storefront payment is not enabled.")
        if not credentials.get("private_key"):
            raise GatewayConfigurationError("Payment processing is not configured.")
        if request.amount < Decimal("5.00"):
            raise GatewayConfigurationError("The purchase amount is below the payment provider minimum.")

    def minimum_checkout_amount(self, credentials):
        return Decimal("5.00")

    def create_checkout(self, request, credentials):
        self.validate_checkout(request, credentials)
        return self._create_external_checkout(request, credentials)

    def checkout_url(self, provider_token, language, credentials):
        token = str(provider_token).strip()
        if not self._TOKEN_RE.fullmatch(token):
            raise GatewayConfigurationError("The secure checkout token is invalid.")
        prefix = "sandbox/" if credentials.get("sandbox") else ""
        safe_language = language if language in {"ar", "en", "fr"} else "en"
        return f"https://youcanpay.com/{prefix}payment-form/{token}?{urlencode({'lang': safe_language})}"

    def resume_checkout_url(self, provider_token, persisted_url, language, credentials):
        # Rebuild deterministic URLs so sandbox/live mode and language cannot
        # be inherited from stale rows or an obsolete deployment environment.
        return self.checkout_url(provider_token, language, credentials)

    def _create_external_checkout(self, request, credentials):
        import requests

        endpoint = self.sandbox_token_endpoint if credentials.get("sandbox") else self.token_endpoint
        data = {
            "pri_key": credentials["private_key"],
            "order_id": request.order_id,
            "amount": str(int((request.amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
            "currency": request.currency,
            "success_url": request.success_url,
            "error_url": request.error_url,
        }
        if request.customer_name:
            data["customer[name]"] = request.customer_name
        if request.customer_email:
            data["customer[email]"] = request.customer_email
        if request.customer_phone:
            data["customer[phone]"] = request.customer_phone
        for key, value in request.metadata.items():
            data[f"metadata[{key}]"] = str(value)
        try:
            response = requests.post(
                endpoint,
                headers={"Accept": "application/json"},
                data=data,
                timeout=int(credentials.get("api_timeout", 10)),
            )
        except requests.RequestException as exc:
            raise GatewayConfigurationError("The secure checkout is temporarily unavailable.") from exc
        if response.status_code not in {200, 201}:
            raise GatewayConfigurationError("The secure checkout could not be prepared.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayConfigurationError("The payment provider returned an invalid response.") from exc
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            raise GatewayConfigurationError("The payment provider returned an invalid response.")
        token_value = payload.get("token") or payload.get("id") or ""
        if isinstance(token_value, Mapping):
            token_value = token_value.get("id", "")
        token = str(token_value).strip()
        if not self._TOKEN_RE.fullmatch(token):
            raise GatewayConfigurationError("The payment provider returned an invalid checkout token.")
        return CheckoutSession(
            provider_token=token,
            payment_url=self.checkout_url(token, request.language, credentials),
        )

    def verify_webhook(self, raw_body, headers, credentials):
        signature = headers.get("X-YouCanPay-Signature") or headers.get("X-YouCan-Signature")
        private_key = credentials.get("private_key")
        if not signature or not private_key:
            return False
        expected = hmac.new(str(private_key).encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, str(signature).strip())

    def parse_webhook(self, payload):
        if not isinstance(payload, Mapping):
            raise InvalidGatewayEvent("Webhook payload must be an object.")
        data = payload.get("payload") or {}
        transaction = data.get("transaction") or {}
        if not isinstance(data, Mapping) or not isinstance(transaction, Mapping):
            raise InvalidGatewayEvent("Invalid payment event structure.")
        event_name = str(payload.get("event_name", "")).strip()
        status = str(transaction.get("status", ""))
        if event_name == "transaction.paid" and status == "1":
            outcome = "paid"
        elif event_name == "transaction.failed":
            outcome = "failed"
        else:
            outcome = "ignored"
        amount_value = transaction.get("amount")
        currency = str(transaction.get("currency", ""))
        if transaction.get("base_amount") is not None and transaction.get("base_currency"):
            amount_value = transaction.get("base_amount")
            currency = str(transaction.get("base_currency"))
        try:
            amount = Decimal(str(amount_value)) / 100
        except Exception:
            amount = None
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise InvalidGatewayEvent("Invalid payment metadata.")
        token = data.get("token") or {}
        provider_token = str(token.get("id", "")).strip() if isinstance(token, Mapping) else ""
        return GatewayEvent(
            provider=self.code,
            event_id=str(payload.get("id", "")).strip(),
            event_name=event_name,
            outcome=outcome,
            order_id=str(transaction.get("order_id", "")).strip(),
            provider_transaction_id=str(transaction.get("id", "")).strip(),
            provider_token=provider_token,
            amount=amount,
            currency=currency,
            sandbox=payload.get("sandbox") if isinstance(payload.get("sandbox"), bool) else None,
            metadata=dict(metadata),
            raw_payload=dict(payload),
        )

    def verify_transaction(self, event, subject, credentials):
        access_token = str(credentials.get("access_token", "")).strip()
        if not access_token:
            if credentials.get("require_transaction_verification"):
                raise GatewayVerificationUnavailable("Provider transaction verification is not configured.")
            return None
        if not event.provider_transaction_id:
            raise InvalidGatewayEvent("Provider transaction ID is required.")

        import requests

        try:
            response = requests.get(
                f"https://youcanpay.com/api/v2/transactions/{event.provider_transaction_id}",
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                timeout=int(credentials.get("api_timeout", 10)),
            )
        except requests.RequestException as exc:
            raise GatewayVerificationUnavailable("Provider transaction lookup failed.") from exc
        if response.status_code != 200:
            raise GatewayVerificationUnavailable("Provider transaction lookup was not available.")
        try:
            verified = response.json()
        except ValueError as exc:
            raise GatewayVerificationUnavailable("Provider transaction response was invalid.") from exc
        if isinstance(verified, dict) and isinstance(verified.get("data"), dict):
            verified = verified["data"]
        if not isinstance(verified, dict):
            raise GatewayVerificationUnavailable("Provider transaction response was invalid.")
        amount_data = verified.get("amount") or {}
        try:
            amount_matches = Decimal(str(amount_data.get("amount"))) == subject.amount
        except Exception:
            amount_matches = False
        expected_live_mode = not bool(credentials.get("sandbox"))
        checks = (
            hmac.compare_digest(str(verified.get("id", "")), event.provider_transaction_id),
            verified.get("status") == "paid",
            str(verified.get("order_id", "")) in {subject.order_id, subject.provider_token},
            amount_matches,
            amount_data.get("currency") == subject.currency,
            verified.get("is_live_mode") is expected_live_mode,
        )
        if not all(checks):
            raise InvalidGatewayEvent("Provider transaction does not match the purchase.")
        metadata = verified.get("metadata") or {}
        for key, expected in subject.metadata.items():
            if metadata.get(key) not in {None, expected}:
                raise InvalidGatewayEvent("Provider transaction metadata does not match.")
        return verified
