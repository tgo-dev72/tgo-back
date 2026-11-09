"""Payment-gateway adapters shared by platform and storefront billing."""

from .base import (
    CheckoutRequest,
    CheckoutSession,
    GatewayConfigurationError,
    GatewayError,
    GatewayPaymentError,
    GatewayEvent,
    GatewayVerificationUnavailable,
    InvalidGatewayEvent,
    PaymentGateway,
    PaymentSubject,
)
from .registry import get_gateway, get_platform_gateway

__all__ = [
    "CheckoutRequest",
    "CheckoutSession",
    "GatewayConfigurationError",
    "GatewayError",
    "GatewayPaymentError",
    "GatewayEvent",
    "GatewayVerificationUnavailable",
    "InvalidGatewayEvent",
    "PaymentGateway",
    "PaymentSubject",
    "get_gateway",
    "get_platform_gateway",
]
