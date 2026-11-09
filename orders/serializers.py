from decimal import Decimal

from rest_framework import serializers

from store.serializers import StoreMemberSerializer
from access_control.services import effective_fields, membership_for

from .models import Order, OrderedProduct, OrderStatusHistory, QuantityException


class OrderItemInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    variant_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    quantity = serializers.IntegerField(min_value=1)


class CheckoutTotalsSerializer(serializers.Serializer):
    subtotal = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    discount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    shipping = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    tax = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False, default=Decimal("0.00"))
    total = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    currency = serializers.CharField(max_length=3)


class CreateOrderSerializer(serializers.Serializer):
    # Optional for older storefront revisions. Current checkout always sends
    # the reviewed quote; these values never determine server pricing.
    expected_totals = CheckoutTotalsSerializer(required=False)
    payment_mode = serializers.ChoiceField(
        choices=[Order.PaymentMode.ONLINE, Order.PaymentMode.COD],
        default=Order.PaymentMode.ONLINE,
    )
    customer = serializers.DictField(
        child=serializers.CharField(allow_blank=True, required=False),
        default=dict,
    )
    items = OrderItemInputSerializer(many=True, allow_empty=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    marketing_consent = serializers.BooleanField(required=False, default=False)
    coupon_code = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=32,
        trim_whitespace=True,
    )

    def validate_customer(self, value):
        limits = {"name": 100, "email": 254, "phone": 20, "city": 50, "address": 1000}
        errors = {key: f"Must be {limit} characters or fewer." for key, limit in limits.items() if len(str(value.get(key, ""))) > limit}
        if errors:
            raise serializers.ValidationError(errors)
        return value


class OrderedProductSerializer(serializers.ModelSerializer):
    attributes = serializers.JSONField()
    exception_id = serializers.UUIDField()

    class Meta:
        model = OrderedProduct
        fields = [
            "id", "product_id", "variant_id", "section_key", "ref", "name",
            "attributes", "quantity", "fulfilled_quantity", "reserved_quantity",
            "price", "available", "stock_deducted",
            "exception_id",
        ]


class CustomerOrderedProductSerializer(serializers.ModelSerializer):
    """Customer-visible line snapshot without internal allocation state."""

    attributes = serializers.JSONField()

    class Meta:
        model = OrderedProduct
        fields = [
            "id", "product_id", "variant_id", "section_key", "ref", "name",
            "attributes", "quantity", "fulfilled_quantity", "price",
        ]
        read_only_fields = fields


class QuantityExceptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuantityException
        fields = ["id", "product_id", "section_key", "ref", "name", "attributes", "delta_quantity", "treated"]


class OrderStatusHistorySerializer(serializers.ModelSerializer):
    changed_by = StoreMemberSerializer(read_only=True)

    class Meta:
        model = OrderStatusHistory
        fields = ["id", "from_status", "to_status", "changed_by", "notes", "created_at"]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    items = OrderedProductSerializer(many=True, read_only=True)
    exceptions = QuantityExceptionSerializer(many=True, read_only=True)
    delivery_person = StoreMemberSerializer(read_only=True)
    status_history = OrderStatusHistorySerializer(many=True, read_only=True)
    invoice = serializers.SerializerMethodField()

    _STATUS_FIX = {"True": "confirmed", "False": "pending", "true": "confirmed", "false": "pending"}

    class Meta:
        model = Order
        fields = [
            "id", "order_id", "amount", "subtotal", "shipping_amount", "tax_amount", "tax_breakdown", "tax_rate", "prices_include_tax", "discount_amount",
            "coupon_code", "coupon_discount_type", "coupon_value",
            "status", "exception", "payment_mode",
            "is_paid", "currency", "invoice_locale",
            "customer_name", "customer_email", "customer_phone",
            "customer_city", "customer_address", "customer_notes",
            "marketing_consent", "loyalty_points_awarded",
            "items_acknowledged_at", "items_acknowledged_by",
            "delivery_person", "tracking_number", "carrier",
            "cancelled_by", "cancel_reason", "cancelled_at",
            "merchant_notes",
            "created_at", "updated_at",
            "confirmed_at", "shipped_at", "delivered_at",
            "items", "exceptions", "status_history", "invoice",
        ]
        read_only_fields = fields

    def get_invoice(self, instance):
        try:
            invoice = instance.issued_invoice
        except Exception:
            return None
        return {"number": invoice.number, "status": invoice.status, "delivery_status": invoice.delivery_status}

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        raw = rep.get("status", "")
        if raw in self._STATUS_FIX:
            rep["status"] = self._STATUS_FIX[raw]
        request = self.context.get("request")
        membership = membership_for(request.user, instance.store) if request is not None else None
        if membership is not None:
            rules = effective_fields(membership).get("orders", {})
            groups = {
                "status": ["status", "cancelled_by", "cancel_reason", "cancelled_at"],
                "customer_identity": ["customer_name"],
                "customer_contact": ["customer_email", "customer_phone"],
                "delivery": ["customer_city", "customer_address", "delivery_person", "tracking_number", "carrier"],
                "totals": ["amount", "subtotal", "shipping_amount", "discount_amount", "coupon_code", "coupon_discount_type", "coupon_value", "currency"],
                "payment": ["payment_mode", "is_paid"],
                "internal_notes": ["merchant_notes", "customer_notes"],
                "items": ["items", "exceptions", "items_acknowledged_at", "items_acknowledged_by"],
            }
            for field, response_fields in groups.items():
                if not rules.get(field, {}).get("view", False):
                    for response_field in response_fields:
                        rep.pop(response_field, None)
        return rep


class CustomerOrderSerializer(serializers.ModelSerializer):
    """Public/customer-safe order representation without PII or internal notes."""

    items = CustomerOrderedProductSerializer(many=True, read_only=True)
    invoice = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "order_id", "amount", "subtotal", "shipping_amount", "tax_amount", "tax_breakdown", "tax_rate", "prices_include_tax", "discount_amount",
            "coupon_code", "coupon_discount_type", "coupon_value",
            "status", "exception", "payment_mode", "is_paid", "currency",
            "tracking_number", "carrier", "cancelled_by", "cancel_reason",
            "cancelled_at", "created_at", "updated_at", "confirmed_at",
            "shipped_at", "delivered_at", "items", "invoice",
        ]
        read_only_fields = fields

    def get_invoice(self, instance):
        try:
            invoice = instance.issued_invoice
        except Exception:
            return None
        return {"number": invoice.number, "status": invoice.status} if invoice.status == invoice.Status.READY else None


class OrderStatusSerializer(serializers.Serializer):
    """Merchant action: confirm a pending order after reviewing every line."""
    order_id = serializers.UUIDField()
    acknowledged_item_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    invoice_locale = serializers.ChoiceField(choices=Order.InvoiceLocale.choices, required=False)


class ShipOrderSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    tracking_number = serializers.CharField(max_length=100, allow_blank=False, trim_whitespace=True)
    carrier = serializers.CharField(max_length=100, allow_blank=False, trim_whitespace=True)
    delivery_person_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class DeliverOrderSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CancelOrderSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class MerchantNoteSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    merchant_notes = serializers.CharField()
