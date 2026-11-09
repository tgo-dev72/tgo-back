from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from .base import PaymentGateway


DEFAULT_BACKENDS = {
    "paypal": "payments.gateways.paypal.PayPalGateway",
    "youcanpay": "payments.gateways.youcanpay.YouCanPayGateway",
}


def configured_backends():
    return getattr(settings, "PAYMENT_GATEWAY_BACKENDS", DEFAULT_BACKENDS)


@lru_cache(maxsize=None)
def _load_gateway(code: str, dotted_path: str) -> PaymentGateway:
    gateway = import_string(dotted_path)()
    if not isinstance(gateway, PaymentGateway):
        raise ImproperlyConfigured(f"Payment gateway '{code}' does not implement PaymentGateway.")
    if gateway.code != code:
        raise ImproperlyConfigured(f"Payment gateway '{dotted_path}' declares code '{gateway.code}', expected '{code}'.")
    return gateway


def get_gateway(code: str) -> PaymentGateway:
    normalized = str(code or "").strip().lower()
    dotted_path = configured_backends().get(normalized)
    if not dotted_path:
        raise ImproperlyConfigured(f"Unsupported payment gateway: {normalized or '(empty)'}.")
    return _load_gateway(normalized, dotted_path)


def get_platform_gateway() -> PaymentGateway:
    return get_gateway(settings.PLATFORM_PAYMENT_GATEWAY)


def gateway_choices():
    return [(code, get_gateway(code).display_name) for code in configured_backends()]


def clear_gateway_cache():
    _load_gateway.cache_clear()
