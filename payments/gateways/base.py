"""Provider-neutral payment contracts.

Business services deal only with these value objects. A provider adapter owns
SDK calls, credential interpretation, signatures, payload normalization, and
optional transaction read-back.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping


class GatewayError(RuntimeError):
    """Base error safe for business services to translate at their boundary."""


class GatewayConfigurationError(GatewayError):
    pass


class InvalidGatewayEvent(GatewayError):
    pass


class GatewayVerificationUnavailable(GatewayError):
    """Provider truth could not be obtained; webhook delivery should retry."""


class GatewayPaymentError(GatewayError):
    """A stable, safe payment failure returned by a provider adapter."""

    def __init__(self, message, code="provider_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CheckoutRequest:
    order_id: str
    amount: Decimal
    currency: str
    success_url: str
    error_url: str
    language: str = "en"
    customer_name: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    customer_address: str = ""
    country_code: str = "MA"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    provider_hosted: bool = False


@dataclass(frozen=True)
class CheckoutSession:
    provider_token: str
    payment_url: str


@dataclass(frozen=True)
class GatewayEvent:
    provider: str
    event_id: str
    event_name: str
    outcome: str
    order_id: str
    provider_transaction_id: str
    provider_token: str
    amount: Decimal | None
    currency: str
    sandbox: bool | None
    metadata: Mapping[str, Any]
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True)
class PaymentSubject:
    order_id: str
    amount: Decimal
    currency: str
    provider_token: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class PaymentGateway(ABC):
    code: str
    display_name: str

    @abstractmethod
    def platform_credentials(self) -> Mapping[str, Any]:
        """Read platform-owned credentials from provider-specific settings."""

    def store_credentials(self, store_config) -> Mapping[str, Any]:
        values = store_config.payment_keys or {}
        provider_values = values.get(self.code) if isinstance(values, dict) else None
        if isinstance(provider_values, dict):
            return provider_values
        # Backward compatibility for rows written before credentials became a
        # per-provider vault.
        if store_config.payment_provider == self.code and isinstance(values, dict):
            return values
        return {}

    @abstractmethod
    def configuration_status(self, credentials: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return provider-neutral availability plus optional diagnostics."""

    @abstractmethod
    def validate_checkout(self, request: CheckoutRequest, credentials: Mapping[str, Any]) -> None:
        pass

    @abstractmethod
    def create_checkout(self, request: CheckoutRequest, credentials: Mapping[str, Any]) -> CheckoutSession:
        pass

    def checkout_url(self, provider_token: str, language: str, credentials: Mapping[str, Any]) -> str:
        """Restore a hosted URL for legacy rows that predate URL persistence."""
        raise GatewayConfigurationError("This checkout cannot be resumed; start a new payment.")

    def resume_checkout_url(
        self,
        provider_token: str,
        persisted_url: str,
        language: str,
        credentials: Mapping[str, Any],
    ) -> str:
        """Return a safe URL for an existing pending checkout.

        Most providers issue opaque URLs that must be persisted. Adapters with
        deterministic URLs may override this to repair an incompatible URL
        after configuration or SDK behavior changes.
        """
        return persisted_url or self.checkout_url(provider_token, language, credentials)

    def minimum_checkout_amount(self, credentials: Mapping[str, Any]) -> Decimal:
        """Smallest externally chargeable amount, in the checkout currency."""
        return Decimal("0.00")

    @abstractmethod
    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, Any], credentials: Mapping[str, Any]) -> bool:
        pass

    @abstractmethod
    def parse_webhook(self, payload: Mapping[str, Any]) -> GatewayEvent:
        pass

    def verify_transaction(
        self,
        event: GatewayEvent,
        subject: PaymentSubject,
        credentials: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Optionally read provider truth before granting value."""
        return None

    def capture_payment(
        self,
        provider_token: str,
        subject: PaymentSubject,
        credentials: Mapping[str, Any],
        idempotency_key: str,
    ) -> GatewayEvent:
        """Capture a merchant-approved provider order.

        Hosted gateways which settle asynchronously need not implement this.
        """
        raise GatewayConfigurationError("This payment provider does not support server-side capture.")

    def credential_fields(self) -> list[dict[str, Any]]:
        """UI schema for merchant-owned credentials; no frontend provider logic."""
        return []

    def public_descriptor(self, credentials: Mapping[str, Any]) -> dict[str, Any]:
        status = dict(self.configuration_status(credentials))
        status.setdefault("checkout_credentials_configured", bool(status.get("configured")))
        status.setdefault("transaction_verification_configured", False)
        status.setdefault("transaction_verification_required", False)
        return {
            "code": self.code,
            "name": self.display_name,
            "configured": bool(status.pop("configured", False)),
            "sandbox": bool(status.pop("sandbox", False)),
            "details": status,
            "credential_fields": self.credential_fields(),
        }
