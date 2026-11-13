"""Tenant-scoped orders — ported from legacy store.Order/OrderedProduct/
QuantityExceptions, generalized to the config-driven catalog (variant-based
stock instead of legacy product+size).

Lifecycle:
  pending  →  confirmed  →  shipped  →  delivered
     ↓            ↓
  cancelled    cancelled

- POST /api/orders/ (public) reserves stock and creates the order.
  - online and cod → status='pending' (stock reserved, not yet deducted).
- confirm_order()  → deducts stock, status='confirmed', awards loyalty.
- ship_order()     → status='shipped', records tracking info.
- deliver_order()  → status='delivered'.
- cancel_order()   → status='cancelled', releases reservations if held.
- fail_order()     → releases reservations, is_paid='failed' (webhook).
"""
import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from store.models import ClientProfile, Store, StoreMember


class Order(models.Model):
    # ── Payment ────────────────────────────────────────────────────────
    class PaymentMode(models.TextChoices):
        ONLINE = "online", "Online"
        COD = "cod", "Cash on delivery"

    class IsPaid(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        COD = "cod", "Cash on delivery"

    # ── Fulfillment status (the real lifecycle) ────────────────────────
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        SHIPPED = "shipped", "Shipped"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    class CancelledBy(models.TextChoices):
        MERCHANT = "merchant", "Merchant"
        CUSTOMER = "customer", "Customer"

    class InvoiceLocale(models.TextChoices):
        FRENCH = "fr", "French"
        ENGLISH = "en", "English"
        ARABIC = "ar", "Arabic"

    # ── Identity ───────────────────────────────────────────────────────
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="orders")
    client = models.ForeignKey(
        ClientProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )
    order_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    transaction_id = models.CharField(max_length=100, blank=True, default="")
    idempotency_key = models.CharField(max_length=100, blank=True, default="", editable=False)
    checkout_fingerprint = models.CharField(max_length=64, blank=True, default="", editable=False)

    # ── Financials ─────────────────────────────────────────────────────
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    shipping_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    tax_breakdown = models.JSONField(default=list, blank=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    prices_include_tax = models.BooleanField(default=True)
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Immutable checkout discount snapshot.",
    )
    coupon_code = models.CharField(max_length=32, blank=True, default="")
    coupon_discount_type = models.CharField(max_length=16, blank=True, default="")
    coupon_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    applied_coupon_id = models.PositiveBigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, default="MAD")
    payment_mode = models.CharField(
        max_length=10, choices=PaymentMode.choices, default=PaymentMode.ONLINE,
    )
    is_paid = models.CharField(
        max_length=10, choices=IsPaid.choices, default=IsPaid.PENDING,
    )
    invoice_locale = models.CharField(
        max_length=2, choices=InvoiceLocale.choices, blank=True, default="",
        help_text="Merchant-selected language frozen before invoice issuance.",
    )

    # ── Fulfillment ────────────────────────────────────────────────────
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING,
    )
    exception = models.BooleanField(default=False)  # True if any item short-fulfilled

    # ── Shipping / tracking ────────────────────────────────────────────
    delivery_person = models.ForeignKey(
        StoreMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_orders",
        limit_choices_to={"role": StoreMember.Roles.DELIVERY},
    )
    tracking_number = models.CharField(max_length=100, blank=True, default="")
    carrier = models.CharField(max_length=100, blank=True, default="")

    # ── Cancellation ───────────────────────────────────────────────────
    cancelled_by = models.CharField(
        max_length=10, choices=CancelledBy.choices, blank=True, default="",
    )
    cancel_reason = models.TextField(blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)

    # ── Notes ──────────────────────────────────────────────────────────
    merchant_notes = models.TextField(
        blank=True, default="",
        help_text="Internal notes visible only to the merchant.",
    )
    customer_notes = models.TextField(
        blank=True, default="",
        help_text="Notes from the customer during checkout.",
    )
    marketing_consent = models.BooleanField(
        default=False,
        help_text="Customer explicitly agreed to receive store campaigns at checkout.",
    )
    marketing_tracking_consent = models.BooleanField(
        default=False,
        help_text="Customer explicitly consented to advertising measurement for this order.",
    )
    marketing_tracking_visitor_hash = models.CharField(
        max_length=64, blank=True, default="", editable=False,
        help_text="One-way tenant-scoped identifier used only for later consent withdrawal.",
    )
    loyalty_points_awarded = models.PositiveIntegerField(
        default=0,
        help_text="Exact points granted by this order, used for safe reversal.",
    )

    # Merchant control: fulfillment cannot be confirmed until every line was
    # reviewed. Payment confirmation remains a separate concern.
    items_acknowledged_at = models.DateTimeField(null=True, blank=True)
    items_acknowledged_by = models.ForeignKey(
        StoreMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acknowledged_orders",
    )

    # ── Customer snapshot (immutable after creation) ────────────────────
    customer_name = models.CharField(max_length=100, blank=True, default="")
    customer_email = models.EmailField(blank=True, default="")
    customer_phone = models.CharField(max_length=20, blank=True, default="")
    customer_city = models.CharField(max_length=50, blank=True, default="")
    customer_address = models.TextField(blank=True, default="")

    # ── Documents ──────────────────────────────────────────────────────
    invoice = models.FileField(null=True, blank=True)
    delivery_form = models.FileField(null=True, blank=True)

    # ── Timestamps ─────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["store", "created_at"], name="order_store_created"),
            models.Index(fields=["store", "status", "created_at"], name="order_store_status_time"),
            models.Index(fields=["store", "payment_mode", "is_paid"], name="order_store_payment"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="uniq_order_checkout_key_per_store",
            ),
        ]

    def __str__(self):
        return str(self.order_id)


class OrderStatusHistory(models.Model):
    """Immutable audit trail — every state transition is recorded."""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="status_history")
    from_status = models.CharField(max_length=12, blank=True, default="")
    to_status = models.CharField(max_length=12)
    changed_by = models.ForeignKey(
        StoreMember,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_status_changes",
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.order} {self.from_status}→{self.to_status}"


class OrderedProduct(models.Model):
    """Immutable snapshot of what was bought (never touched by later edits)."""
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="ordered_products")
    product_id = models.PositiveIntegerField(editable=False)
    variant_id = models.PositiveIntegerField(editable=False)
    section_key = models.CharField(max_length=50)
    ref = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    attributes = models.JSONField(default=dict, blank=True)
    quantity = models.PositiveIntegerField()
    fulfilled_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Quantity allocated to this order when it was placed.",
    )
    reserved_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Current variant reservation owned by this order line.",
    )
    price = models.DecimalField(
        max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    available = models.BooleanField(default=True)
    stock_deducted = models.BooleanField(default=False)
    exception_id = models.UUIDField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["store", "product_id"], name="order_line_store_product"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(fulfilled_quantity__lte=models.F("quantity")),
                name="order_line_fulfilled_not_above_requested",
            ),
            models.CheckConstraint(
                condition=models.Q(reserved_quantity__lte=models.F("fulfilled_quantity")),
                name="order_line_reserved_not_above_fulfilled",
            ),
        ]

    def __str__(self):
        return f"{self.order} / {self.ref} × {self.quantity}"


class QuantityException(models.Model):
    """Stock shortfall recorded at order time (undeliverable portion)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="quantity_exceptions")
    client = models.ForeignKey(ClientProfile, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="exceptions")
    product_id = models.PositiveIntegerField()
    section_key = models.CharField(max_length=50)
    ref = models.CharField(max_length=100)
    name = models.CharField(max_length=100)
    attributes = models.JSONField(default=dict, blank=True)
    delta_quantity = models.PositiveIntegerField()
    treated = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.order} / {self.ref} short by {self.delta_quantity}"


class InvoiceSeries(models.Model):
    """Locked counter shared by every store using the same approved issuer scope."""

    issuer_scope = models.CharField(max_length=64)
    prefix = models.CharField(max_length=20)
    year = models.PositiveSmallIntegerField()
    last_number = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issuer_scope", "prefix", "year"], name="uniq_invoice_issuer_series_year"),
        ]


class Invoice(models.Model):
    class Status(models.TextChoices):
        ISSUED = "issued", "Issued"
        RENDERING = "rendering", "Rendering"
        READY = "ready", "Ready"
        NEEDS_REVIEW = "needs_review", "Needs review"
        VOID = "void", "Void"

    class DeliveryStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="issued_invoice")
    store = models.ForeignKey(Store, on_delete=models.PROTECT, related_name="invoices")
    series = models.ForeignKey(InvoiceSeries, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField(max_length=64)
    sequence = models.PositiveBigIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ISSUED)
    template_version = models.CharField(max_length=20, default="fr-v1")
    issued_at = models.DateTimeField(default=timezone.now)
    snapshot = models.JSONField(default=dict)
    pdf = models.FileField(upload_to="private/invoices/%Y/%m", blank=True, default="")
    storage_public_id = models.CharField(max_length=255, blank=True, default="")
    storage_version = models.CharField(max_length=64, blank=True, default="")
    delivery_status = models.CharField(max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING)
    delivery_attempts = models.PositiveIntegerField(default=0)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["series", "sequence"], name="uniq_invoice_sequence_per_series"),
            models.UniqueConstraint(fields=["series", "number"], name="uniq_invoice_number_per_series"),
        ]

    def __str__(self):
        return self.number
