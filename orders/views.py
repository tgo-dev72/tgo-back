import hashlib
import json
from decimal import Decimal
from urllib.error import URLError
from urllib.request import urlopen

from django.core import signing
from django.http import FileResponse, HttpResponse
from django.utils import timezone

from django.core.serializers.json import DjangoJSONEncoder
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import ClientCookieJWTAuthentication
from access_control.services import membership_for, require_permission
from store.access import OPERATIONS_ROLES
from store.context import resolve_store
from store.models import Store, StoreConfig, StoreMember

from .models import Invoice, Order
from .invoices import InvoiceError, capture_storefront_logo, issue_render_and_deliver, render_invoice_pdf, signed_invoice_url
from .serializers import (
    CancelOrderSerializer,
    CreateOrderSerializer,
    CustomerOrderSerializer,
    DeliverOrderSerializer,
    MerchantNoteSerializer,
    OrderSerializer,
    OrderStatusSerializer,
    ShipOrderSerializer,
)
from .services import (
    _tax_totals,
    CheckoutPriceChangedError,
    IdempotencyConflictError,
    InvalidCouponError,
    InvalidOrderItemError,
    InvalidTransitionError,
    cancel_order,
    confirm_order,
    create_order,
    quote_order,
    deliver_order,
    fail_order,
    ship_order,
    update_notes,
)


def _merchant_stores(user):
    return Store.objects.filter(members__user=user)


def _merchant_member(user, store_id):
    """Return the StoreMember row for this user in the given store."""
    return StoreMember.objects.filter(user=user, store_id=store_id).first()


class PublicOrderQuoteView(APIView):
    """Price the current cart without creating or reserving anything."""
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        if store is None:
            return Response({"detail": "Unknown store."}, status=status.HTTP_404_NOT_FOUND)
        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            quote = quote_order(store, data["customer"], data["items"], request, data["coupon_code"])
        except InvalidCouponError as exc:
            return Response({"coupon_code": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except InvalidOrderItemError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(quote)


class PublicOrderCreateView(APIView):
    """POST /api/orders/ — storefront checkout."""
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        if store is None:
            return Response({"detail": "Unknown store."}, status=status.HTTP_404_NOT_FOUND)

        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data["payment_mode"] == "cod" and not store.config.cod_enabled:
            return Response(
                {"payment_mode": ["Cash on delivery is not available for this store."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if len(idempotency_key) > 100:
            return Response(
                {"detail": "The checkout request identifier is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        checkout_fingerprint = hashlib.sha256(
            json.dumps(request.data, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest() if idempotency_key else ""

        try:
            order = create_order(
                store=store,
                customer=data["customer"],
                items=data["items"],
                payment_mode=data["payment_mode"],
                request=request,
                notes=data.get("notes", ""),
                marketing_consent=data.get("marketing_consent", False),
                marketing_tracking_consent=data.get("marketing_tracking_consent", False),
                marketing_tracking_id=data.get("marketing_tracking_id", ""),
                coupon_code=data.get("coupon_code", ""),
                idempotency_key=idempotency_key,
                checkout_fingerprint=checkout_fingerprint,
                expected_totals=data.get("expected_totals"),
            )
        except CheckoutPriceChangedError as exc:
            return Response({"code": "checkout_changed", "detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except IdempotencyConflictError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except InvalidCouponError as exc:
            return Response({"coupon_code": [str(exc)]}, status=status.HTTP_400_BAD_REQUEST)
        except InvalidOrderItemError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        payload = CustomerOrderSerializer(order).data
        if order.client_id is None and order.customer_email:
            payload["guest_invoice_token"] = signing.dumps(
                {"order": str(order.order_id), "email": order.customer_email}, salt="guest-invoice",
            )
        return Response(payload, status=status.HTTP_201_CREATED)


class PublicOrderDetailView(APIView):
    """GET /api/orders/<order_id>/ — customer status check."""
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, order_id):
        store = resolve_store(request)
        if store is None:
            return Response({"detail": "Unknown store."}, status=status.HTTP_404_NOT_FOUND)

        try:
            order = Order.objects.prefetch_related(
                "items", "exceptions", "status_history",
            ).get(store=store, order_id=order_id)
        except (Order.DoesNotExist, ValueError):
            return Response({"found": False}, status=status.HTTP_200_OK)
        if order.client_id and (
            not request.user.is_authenticated or order.client.user_id != request.user.id
        ):
            return Response({"found": False}, status=status.HTTP_200_OK)
        return Response(CustomerOrderSerializer(order).data)


class PublicOrderCancelView(APIView):
    """POST /api/orders/<order_id>/cancel/ — customer self-cancel (pending only)."""
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        store = resolve_store(request)
        if store is None:
            return Response({"detail": "Unknown store."}, status=status.HTTP_404_NOT_FOUND)

        try:
            order = Order.objects.get(store=store, order_id=order_id)
        except (Order.DoesNotExist, ValueError):
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        if order.client_id and (
            not request.user.is_authenticated or order.client.user_id != request.user.id
        ):
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        if order.status != Order.Status.PENDING:
            return Response(
                {"detail": "Only pending orders can be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.payment_mode == Order.PaymentMode.ONLINE and order.is_paid == Order.IsPaid.CONFIRMED:
            return Response(
                {"detail": "This order can no longer be cancelled online. Please contact the store."},
                status=status.HTTP_409_CONFLICT,
            )

        reason = request.data.get("reason", "")
        try:
            order = cancel_order(
                order,
                cancelled_by=Order.CancelledBy.CUSTOMER,
                reason=reason,
            )
        except InvalidTransitionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CustomerOrderSerializer(order).data)


class CustomerOrderListView(generics.ListAPIView):
    """GET /api/orders/mine/ — current customer's orders for this store."""

    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = CustomerOrderSerializer
    pagination_class = None

    def get_queryset(self):
        store = resolve_store(self.request)
        if store is None:
            return Order.objects.none()
        return (
            Order.objects.filter(store=store, client__user=self.request.user)
            .prefetch_related("items")
            .order_by("-created_at")
        )


class MerchantOrderListView(generics.ListAPIView):
    """GET /api/store/<store_id>/orders/ — merchant order inbox."""
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        store = generics.get_object_or_404(_merchant_stores(self.request.user), pk=self.kwargs["store_id"])
        membership = require_permission(self.request.user, store, "orders.view")
        if membership.role == StoreMember.Roles.DELIVERY:
            orders = Order.objects.filter(store=store, delivery_person=membership)
        elif membership.role in OPERATIONS_ROLES:
            orders = Order.objects.filter(store=store)
        else:
            return Order.objects.none()
        return (
            orders
            .select_related("store", "delivery_person", "delivery_person__user")
            .prefetch_related("items", "exceptions", "status_history")
        )


class MerchantOrderActionView(APIView):
    """POST /api/store/<store_id>/orders/<action>/ — confirm|fail|ship|deliver|cancel|notes."""
    permission_classes = [IsAuthenticated]

    def post(self, request, store_id, action):
        store = generics.get_object_or_404(_merchant_stores(request.user), pk=store_id)
        required = {
            "confirm": "orders.update", "fail": "orders.update", "notes": "orders.update",
            "ship": "orders.fulfill", "deliver": "orders.fulfill", "cancel": "orders.cancel",
        }.get(action)
        if required is None:
            return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)
        member = require_permission(request.user, store, required)

        try:
            order = Order.objects.get(
                store=store,
                order_id=request.data.get("order_id"),
            )
        except (Order.DoesNotExist, ValueError, KeyError):
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        if member.role == StoreMember.Roles.DELIVERY:
            if action != "deliver" or order.delivery_person_id != member.id:
                return Response(
                    {"detail": "Delivery members can only complete orders assigned to them."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        try:
            if action == "confirm":
                serializer = OrderStatusSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                order = confirm_order(
                    order,
                    changed_by=member,
                    acknowledged_item_ids=serializer.validated_data["acknowledged_item_ids"],
                    invoice_locale=serializer.validated_data.get("invoice_locale"),
                )

            elif action == "fail":
                if order.is_paid != Order.IsPaid.PENDING:
                    return Response(
                        {"detail": "Only pending orders can be failed."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                order = fail_order(order)
                from .models import OrderStatusHistory
                _record_transition_fallback(order, "", "failed", member)

            elif action == "ship":
                serializer = ShipOrderSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                data = serializer.validated_data
                order = ship_order(
                    order,
                    changed_by=member,
                    tracking_number=data.get("tracking_number", ""),
                    carrier=data.get("carrier", ""),
                    delivery_person_id=data.get("delivery_person_id"),
                    notes=data.get("notes", ""),
                )

            elif action == "deliver":
                order = deliver_order(order, changed_by=member)

            elif action == "cancel":
                serializer = CancelOrderSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                order = cancel_order(
                    order,
                    cancelled_by=Order.CancelledBy.MERCHANT,
                    reason=serializer.validated_data.get("reason", ""),
                    changed_by=member,
                )

            elif action == "notes":
                serializer = MerchantNoteSerializer(data=request.data)
                serializer.is_valid(raise_exception=True)
                order = update_notes(order, merchant_notes=serializer.validated_data["merchant_notes"])

            else:
                return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)

        except InvalidTransitionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(OrderSerializer(order).data)


class MerchantOrderDetailView(generics.RetrieveAPIView):
    """Merchant: a dedicated, tenant-scoped order detail endpoint."""

    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "order_id"
    lookup_url_kwarg = "order_id"

    def get_queryset(self):
        store = generics.get_object_or_404(
            _merchant_stores(self.request.user),
            pk=self.kwargs["store_id"],
        )
        member = require_permission(self.request.user, store, "orders.view")
        if member.role == StoreMember.Roles.DELIVERY:
            orders = Order.objects.filter(store=store, delivery_person=member)
        elif member.role in OPERATIONS_ROLES:
            orders = Order.objects.filter(store=store)
        else:
            return Order.objects.none()
        return orders.select_related(
            "delivery_person", "delivery_person__user", "items_acknowledged_by",
            "items_acknowledged_by__user",
        ).prefetch_related("items", "exceptions", "status_history")


def _record_transition_fallback(order, from_status, to_status, member):
    """Fallback for fail_order which doesn't go through the normal transition."""
    from .models import OrderStatusHistory
    OrderStatusHistory.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        changed_by=member,
    )


def _invoice_response(invoice):
    if invoice.pdf:
        invoice.pdf.open("rb")
        response = FileResponse(invoice.pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{invoice.number}.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response
    url = signed_invoice_url(invoice)
    if not url:
        return Response({"detail": "Invoice PDF is not ready."}, status=status.HTTP_409_CONFLICT)
    try:
        with urlopen(url, timeout=20) as stored:
            data = stored.read(10 * 1024 * 1024 + 1)
    except (OSError, URLError):
        return Response({"detail": "Invoice PDF storage is temporarily unavailable."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if len(data) > 10 * 1024 * 1024 or not data.startswith(b"%PDF-"):
        return Response({"detail": "Invoice PDF storage returned an invalid file."}, status=status.HTTP_502_BAD_GATEWAY)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice.number}.pdf"'
    response["Content-Length"] = str(len(data))
    response["Cache-Control"] = "private, no-store"
    return response


class MerchantInvoicePreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, store_id):
        store = generics.get_object_or_404(_merchant_stores(request.user), pk=store_id)
        require_permission(request.user, store, "settings.view")
        from subscriptions.entitlements import owner_has_store_entitlement
        from subscriptions.policies import INVOICING_KEY

        if not owner_has_store_entitlement(store, INVOICING_KEY):
            raise PermissionDenied("Invoice issuance is not included in the store owner's current plan.")
        config = StoreConfig.objects.get(store=store)
        subtotal = Decimal("100.00")
        shipping = Decimal(str(config.shipping_fee))
        tax, tax_breakdown = _tax_totals(config, subtotal, shipping)
        total = subtotal + shipping + (tax if not config.invoice_prices_include_tax else Decimal("0.00"))
        sample = {
            "seller": {
                "legal_name": config.invoice_legal_name or store.name,
                "address": config.invoice_billing_address or "Adresse de facturation",
                "country": config.invoice_country or "Maroc",
                "email": config.invoice_contact_email or config.contact_email,
                "identifiers": {key.upper(): value for key, value in (config.invoice_identifiers or {}).items()},
                "tax_rate": str(config.invoice_tax_rate) if config.invoice_tax_rate is not None else None,
                "prices_include_tax": config.invoice_prices_include_tax,
                "tax_wording": config.invoice_tax_wording,
                "logo_asset": capture_storefront_logo(store),
            },
            "buyer": {"name": "Client exemple", "address": "24, rue des Orangers", "city": "Casablanca", "email": "client@example.test"},
            "order": {"id": "APERÇU", "date": timezone.now().isoformat()},
            "currency": config.currency,
            "lines": [{"name": "Article exemple", "reference": "SKU-001", "quantity": 2, "unit_price": "50.00", "tax_rate": str(config.invoice_tax_rate or 0), "line_total": "100.00"}],
            "totals": {"subtotal": str(subtotal), "discount": "0.00", "shipping": str(shipping), "tax": str(tax), "tax_breakdown": tax_breakdown, "total": str(total)},
        }
        pdf = render_invoice_pdf(sample, specimen=True)
        response = HttpResponse(pdf, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="invoice-preview.pdf"'
        response["Cache-Control"] = "private, no-store"
        return response


class MerchantInvoiceDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, store_id, order_id):
        store = generics.get_object_or_404(_merchant_stores(request.user), pk=store_id)
        require_permission(request.user, store, "orders.view")
        invoice = generics.get_object_or_404(Invoice.objects.select_related("order"), store=store, order__order_id=order_id, status=Invoice.Status.READY)
        return _invoice_response(invoice)

    def post(self, request, store_id, order_id):
        store = generics.get_object_or_404(_merchant_stores(request.user), pk=store_id)
        require_permission(request.user, store, "orders.update")
        order = generics.get_object_or_404(Order, store=store, order_id=order_id)
        try:
            invoice = issue_render_and_deliver(order)
        except InvoiceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"number": invoice.number, "status": invoice.status, "delivery_status": invoice.delivery_status})


class CustomerInvoiceDownloadView(APIView):
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [AllowAny]

    def get(self, request, order_id, store_slug=None):
        store = Store.objects.filter(slug=store_slug).first() if store_slug else resolve_store(request)
        if store is None:
            return Response({"detail": "Unknown store."}, status=status.HTTP_404_NOT_FOUND)
        invoice = generics.get_object_or_404(Invoice.objects.select_related("order", "order__client"), store=store, order__order_id=order_id, status=Invoice.Status.READY)
        order = invoice.order
        allowed = bool(order.client_id and request.user.is_authenticated and order.client.user_id == request.user.id)
        if not allowed and order.client_id is None:
            token = request.query_params.get("token", "")
            try:
                payload = signing.loads(token, salt="guest-invoice", max_age=60 * 60 * 24 * 7)
                allowed = payload == {"order": str(order.order_id), "email": order.customer_email}
            except signing.BadSignature:
                allowed = False
        if not allowed:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        return _invoice_response(invoice)
