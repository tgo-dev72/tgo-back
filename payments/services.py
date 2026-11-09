"""Provider-neutral storefront payment orchestration.

This module owns order and inventory state. Gateway adapters own SDK calls,
credentials, signatures, payload formats, hosted-checkout URLs, and provider
transaction semantics.
"""

import hmac
import traceback
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction

from orders.services import InvalidTransitionError, fail_order, reserve_order_for_retry
from payments.gateways import CheckoutRequest, GatewayError, GatewayEvent, InvalidGatewayEvent, get_gateway

from .models import PaymentTransaction


class PaymentError(RuntimeError):
    pass


class PaymentConfigError(PaymentError):
    pass


class InvalidWebhookError(PaymentError):
    pass


def gateway_context(store, provider=None):
    from store.models import StoreConfig

    config = StoreConfig.objects.get(store=store)
    provider_code = provider or config.payment_provider or settings.STOREFRONT_DEFAULT_PAYMENT_GATEWAY
    gateway = get_gateway(provider_code)
    return gateway, gateway.store_credentials(config)


def create_payment_url(store, order, success_url, error_url, lang="en"):
    """Create a hosted checkout while keeping provider details out of orders."""
    gateway, credentials = gateway_context(store)
    def return_url(url):
        parts = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "order_id"]
        query.append(("order_id", str(order.order_id)))
        return urlunsplit(parts._replace(query=urlencode(query)))

    request = CheckoutRequest(
        order_id=str(order.order_id),
        amount=order.amount,
        currency=order.currency,
        success_url=return_url(success_url),
        error_url=return_url(error_url),
        provider_hosted=True,
        language=lang,
        customer_name=order.customer_name or "Storefront customer",
        customer_email=order.customer_email or "",
        customer_phone=order.customer_phone or "",
        customer_address=order.customer_address or "",
        metadata={"purpose": "storefront_order", "store_id": store.pk},
    )
    try:
        gateway.validate_checkout(request, credentials)
    except GatewayError as exc:
        raise PaymentConfigError(str(exc)) from exc

    payment = PaymentTransaction.objects.create(
        store=store,
        order=order,
        provider=gateway.code,
        amount=order.amount,
        currency=order.currency,
        status=PaymentTransaction.Status.PENDING,
    )
    try:
        session = gateway.create_checkout(request, credentials)
        if not session.provider_token or not session.payment_url:
            raise PaymentConfigError("The secure checkout could not be prepared.")
        payment.provider_token = session.provider_token
        payment.save(update_fields=["provider_token", "updated_at"])
        return session.payment_url, payment
    except GatewayError as exc:
        fail_order(order)
        payment.status = PaymentTransaction.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])
        raise PaymentConfigError("Online payment is not available for this store.") from exc
    except Exception:
        fail_order(order)
        payment.status = PaymentTransaction.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])
        traceback.print_exc()
        raise


def retry_payment_url(store, order, success_url, error_url, lang="en"):
    """Reset order state under a short lock, then call the gateway unlocked."""
    with transaction.atomic():
        order = order.__class__.objects.select_for_update().get(pk=order.pk)
        if order.payment_mode != order.PaymentMode.ONLINE:
            raise PaymentError("Not an online order.")
        if order.is_paid in (order.IsPaid.CONFIRMED, order.IsPaid.COD):
            raise PaymentError("Order is already treated.")
        try:
            order = reserve_order_for_retry(order)
        except InvalidTransitionError as exc:
            raise PaymentError("This order can no longer be retried.") from exc
    try:
        return create_payment_url(store, order, success_url, error_url, lang)
    except PaymentError:
        fail_order(order)
        raise
    except Exception:
        fail_order(order)
        traceback.print_exc()
        raise


@transaction.atomic
def cancel_payment(order):
    if order.payment_mode != order.PaymentMode.ONLINE:
        raise PaymentError("Not an online order.")
    if order.is_paid != order.IsPaid.PENDING:
        raise PaymentError("Order not pending — already treated.")
    fail_order(order)


@transaction.atomic
def handle_webhook(store, order, event: GatewayEvent):
    """Apply a normalized gateway event to an order exactly once."""
    if not isinstance(event, GatewayEvent):
        payment_hint = PaymentTransaction.objects.filter(store=store, order=order).order_by("-created_at").first()
        if payment_hint is None:
            raise InvalidWebhookError("No matching payment transaction exists.")
        try:
            event = get_gateway(payment_hint.provider).parse_webhook(event)
        except InvalidGatewayEvent as exc:
            raise InvalidWebhookError(str(exc)) from exc
    if event.outcome not in {"paid", "failed", "ignored"}:
        raise InvalidWebhookError("Invalid payment outcome.")
    original_order = order
    order = order.__class__.objects.select_for_update().get(pk=order.pk)
    payment = PaymentTransaction.objects.select_for_update().filter(
        store=store,
        order=order,
        provider=event.provider,
    ).order_by("-created_at").first()
    if payment is None:
        raise InvalidWebhookError("No matching payment transaction exists.")
    if not event.provider_token or not hmac.compare_digest(payment.provider_token, event.provider_token):
        raise InvalidWebhookError("Payment reference does not match.")
    if event.order_id != str(order.order_id):
        raise InvalidWebhookError("Payment order does not match.")
    if event.amount != order.amount or event.currency != order.currency:
        raise InvalidWebhookError("Payment amount or currency does not match.")
    gateway, credentials = gateway_context(store, event.provider)
    expected_sandbox = bool(gateway.public_descriptor(credentials)["sandbox"])
    if event.sandbox is not None and event.sandbox != expected_sandbox:
        raise InvalidWebhookError("Payment environment does not match.")
    if not event.provider_transaction_id:
        raise InvalidWebhookError("Provider transaction ID is required.")
    if PaymentTransaction.objects.exclude(pk=payment.pk).filter(
        provider=event.provider,
        provider_transaction_id=event.provider_transaction_id,
    ).exists():
        raise InvalidWebhookError("Provider transaction belongs to another payment.")

    if event.outcome == "ignored":
        return payment
    paid = event.outcome == "paid"
    if order.payment_mode == order.PaymentMode.ONLINE and order.is_paid == order.IsPaid.PENDING:
        if paid:
            # Payment and fulfillment are separate states. A successful charge
            # marks the order paid, but the merchant must still review every
            # line before confirming and deducting reserved inventory.
            order.is_paid = order.IsPaid.CONFIRMED
            order.transaction_id = event.provider_transaction_id
            order.save(update_fields=["is_paid", "transaction_id", "updated_at"])
        else:
            fail_order(order)

    if payment.status == PaymentTransaction.Status.PENDING:
        payment.status = PaymentTransaction.Status.CONFIRMED if paid else PaymentTransaction.Status.FAILED
        payment.provider_transaction_id = event.provider_transaction_id
        payment.webhook_payload = dict(event.raw_payload)
        payment.save()
    original_order.refresh_from_db()
    return payment


def verify_status(order):
    if order.is_paid in (order.IsPaid.CONFIRMED, order.IsPaid.COD):
        return "confirmed"
    if order.is_paid == order.IsPaid.FAILED:
        return "failed"
    return "pending"
