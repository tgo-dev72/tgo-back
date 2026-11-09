from django.conf import settings

from payments.models import PlatformPaymentGateway

from .registry import configured_backends, get_gateway


_UNSET = object()


def platform_gateway_descriptors():
    overrides = {
        item.code: item
        for item in PlatformPaymentGateway.objects.filter(code__in=configured_backends())
    }
    descriptors = []
    for code in configured_backends():
        gateway = get_gateway(code)
        setting = overrides.get(code)
        credentials = effective_platform_credentials(gateway, setting)
        descriptor = gateway.public_descriptor(credentials)
        descriptor["enabled"] = (
            setting.is_enabled if setting is not None else code == settings.PLATFORM_PAYMENT_GATEWAY
        )
        descriptor["sandbox_configurable"] = "sandbox" in gateway.platform_credentials()
        descriptor.pop("credential_fields", None)
        descriptors.append(descriptor)
    return descriptors


def available_platform_gateways():
    return [item for item in platform_gateway_descriptors() if item["enabled"] and item["configured"]]


def get_available_platform_gateway(code=None):
    available = available_platform_gateways()
    requested = str(code or "").strip().lower()
    if requested:
        match = next((item for item in available if item["code"] == requested), None)
        if match is None:
            raise ValueError("This payment method is not available.")
        return get_gateway(requested)
    preferred = next((item for item in available if item["code"] == settings.PLATFORM_PAYMENT_GATEWAY), None)
    if preferred:
        return get_gateway(preferred["code"])
    if available:
        return get_gateway(available[0]["code"])
    raise ValueError("No payment method is currently available.")


def effective_platform_credentials(gateway, setting=_UNSET):
    credentials = dict(gateway.platform_credentials())
    if setting is _UNSET:
        setting = PlatformPaymentGateway.objects.filter(code=gateway.code).first()
    if setting is not None and "sandbox" in credentials:
        credentials["sandbox"] = setting.sandbox_enabled
    return credentials
