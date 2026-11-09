from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from catalog.models import CatalogSection, Product
from store.models import Store


class Coupon(models.Model):
    """A merchant-managed, store-scoped checkout discount definition."""

    class DiscountType(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage"
        FIXED = "fixed", "Fixed amount"

    class Scope(models.TextChoices):
        ALL_PRODUCTS = "all_products", "All products"
        CATEGORIES = "categories", "Selected categories"
        PRODUCTS = "products", "Selected products"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="coupons")
    code = models.CharField(
        max_length=32,
        validators=[
            RegexValidator(
                r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
                "Use letters, numbers, hyphens, or underscores.",
            )
        ],
    )
    discount_type = models.CharField(max_length=16, choices=DiscountType.choices)
    value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.ALL_PRODUCTS)
    categories = models.ManyToManyField(CatalogSection, blank=True, related_name="coupons")
    products = models.ManyToManyField(Product, blank=True, related_name="coupons")
    minimum_subtotal = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    maximum_discount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    total_usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    per_customer_usage_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    usage_count = models.PositiveIntegerField(default=0, editable=False)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "code"], name="uniq_coupon_code_per_store"),
            models.CheckConstraint(condition=Q(value__gt=0), name="coupon_value_positive"),
            models.CheckConstraint(condition=Q(minimum_subtotal__gte=0), name="coupon_minimum_nonnegative"),
            models.CheckConstraint(
                condition=Q(discount_type="fixed") | Q(value__lte=100),
                name="coupon_percentage_at_most_100",
            ),
        ]

    def clean(self):
        super().clean()
        self.code = (self.code or "").strip().upper()
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "The end must be later than the start."})
        if self.discount_type == self.DiscountType.FIXED and self.maximum_discount is not None:
            raise ValidationError({"maximum_discount": "A maximum applies only to percentage discounts."})

    @property
    def availability(self):
        now = timezone.now()
        if not self.is_active:
            return "paused"
        if self.total_usage_limit is not None and self.usage_count >= self.total_usage_limit:
            return "exhausted"
        if self.starts_at and self.starts_at > now:
            return "scheduled"
        if self.ends_at and self.ends_at <= now:
            return "expired"
        return "active"

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.store.slug} / {self.code}"


class CouponRedemption(models.Model):
    """Durable usage allocation for one order.

    A redemption is reserved when checkout creates the order, consumed when
    the order is confirmed, and released when payment fails or the order is
    cancelled. The order keeps its own immutable commercial snapshot even if
    the coupon is later changed or deleted.
    """

    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CONSUMED = "consumed", "Consumed"
        RELEASED = "released", "Released"

    coupon = models.ForeignKey(
        Coupon,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="redemptions",
    )
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="coupon_redemption",
    )
    customer_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    discount_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RESERVED)
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["coupon", "customer_key", "status"], name="coupon_customer_usage_idx"),
        ]

    def __str__(self):
        return f"{self.order_id} / {self.status}"
