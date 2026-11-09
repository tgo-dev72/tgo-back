from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.exceptions import ImproperlyConfigured
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.models import Order
from store.context import resolve_store
from payments.gateways import InvalidGatewayEvent, get_gateway
from payments.gateways.registry import configured_backends

from .services import (
    PaymentError,
    InvalidWebhookError,
    cancel_payment,
    create_payment_url,
    gateway_context,
    handle_webhook,
    retry_payment_url,
    verify_status,
)


class _PaymentParams:
    """Lightweight shared parsing of the storefront payment requests."""

    def __init__(self, request):
        data = request.data if hasattr(request, "data") else {}
        self.order_id = str(data.get("order_id", "")).strip()
        self.transaction_id = str(data.get("transaction_id", "")).strip()
        token_params = data.get("tokenParams", {}) or {}
        self.success_url = token_params.get("success_url") or data.get("success_url")
        self.error_url = token_params.get("error_url") or data.get("error_url")
        self.lang = token_params.get("lang", "en") or "en"


def _get_store_order(store, order_id):
    if not order_id:
        return None
    try:
        return Order.objects.get(store=store, order_id=order_id)
    except (Order.DoesNotExist, ValueError, DjangoValidationError):
        return None


class CreatePaymentView(APIView):
    """POST /api/payments/ — storefront requests a hosted payment URL for a
    pending online order (stock already reserved at POST /api/orders/)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        params = _PaymentParams(request)
        order = _get_store_order(store, params.order_id)

        if order is None or order.payment_mode != Order.PaymentMode.ONLINE or order.is_paid != Order.IsPaid.PENDING:
            return Response({"detail": "Order not found or not payable."}, status=status.HTTP_400_BAD_REQUEST)
        if not params.success_url or not params.error_url:
            return Response({"detail": "success_url and error_url are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payment_url, payment = create_payment_url(store, order, params.success_url, params.error_url, params.lang)
        except PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"detail": "Payment is temporarily unavailable. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({
            "payment_url": payment_url,
            "order_id": str(order.order_id),
            "amount": payment.amount,
            "currency": payment.currency,
        })


class RetryPaymentView(APIView):
    """POST /api/payments/retry/ — fresh URL for the same order/reservation."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        params = _PaymentParams(request)
        order = _get_store_order(store, params.order_id)

        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)
        if not params.success_url or not params.error_url:
            return Response({"detail": "success_url and error_url are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payment_url, payment = retry_payment_url(store, order, params.success_url, params.error_url, params.lang)
        except PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except Exception:
            return Response({"detail": "Payment is temporarily unavailable. Please try again."}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"payment_url": payment_url, "order_id": str(order.order_id)})


class CancelPaymentView(APIView):
    """POST /api/payments/cancel/ — mark failed and release the reservation."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        params = _PaymentParams(request)
        order = _get_store_order(store, params.order_id)

        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            cancel_payment(order)
        except PaymentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_423_LOCKED)

        return Response({"message": "Order cancelled and stock released."})


class VerifyPaymentView(APIView):
    """POST /api/payments/verify/ — storefront polls until confirmed/failed."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        params = _PaymentParams(request)

        if not params.order_id:
            return Response({"verified": False, "status": "error", "reason": "Missing order_id."})

        order = _get_store_order(store, params.order_id)
        if order is None:
            return Response({"verified": False, "status": "pending", "reason": "Order not found yet."})

        if params.transaction_id and order.transaction_id != params.transaction_id:
            return Response({"verified": False, "status": "pending", "reason": "Payment not confirmed yet."})

        status_value = verify_status(order)
        return Response({
            "verified": status_value == "confirmed",
            "status": status_value,
            "order_id": str(order.order_id),
        })


class PaymentWebhookView(APIView):
    """Provider webhook normalized through the selected gateway adapter.

    Store is resolved *via the order*, never from the request, so a webhook
    can only ever confirm/fail its own store's order."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider=None):
        try:
            raw_body = request.body
            payload = request.data
            provider_codes = [provider] if provider else [
                settings.STOREFRONT_DEFAULT_PAYMENT_GATEWAY,
                *(code for code in configured_backends() if code != settings.STOREFRONT_DEFAULT_PAYMENT_GATEWAY),
            ]
            gateway = event = order = None
            matching_order_found = False
            for provider_code in provider_codes:
                try:
                    candidate = get_gateway(provider_code)
                except ImproperlyConfigured:
                    continue
                try:
                    candidate_event = candidate.parse_webhook(payload)
                except InvalidGatewayEvent:
                    continue
                if not candidate_event.order_id:
                    continue
                try:
                    candidate_order = Order.objects.select_related("store").get(order_id=candidate_event.order_id)
                except (Order.DoesNotExist, ValueError, DjangoValidationError):
                    continue
                matching_order_found = True
                configured_gateway, credentials = gateway_context(candidate_order.store, candidate.code)
                if configured_gateway.code != candidate.code or not candidate.verify_webhook(raw_body, request.headers, credentials):
                    continue
                gateway, event, order = candidate, candidate_event, candidate_order
                break
            if gateway is None or event is None or order is None:
                if matching_order_found:
                    return Response({"received": False, "error": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)
                return Response({"received": True}, status=200)
            handle_webhook(order.store, order, event)
            return Response({"received": True}, status=200)
        except Order.DoesNotExist:
            return Response({"received": True}, status=200)  # ack; nothing to do
        except (InvalidWebhookError, InvalidGatewayEvent, ImproperlyConfigured):
            return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"received": False}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
