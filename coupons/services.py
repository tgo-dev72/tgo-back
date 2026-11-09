"""Transactional coupon pricing and usage accounting."""

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from catalog.models import Product

from .models import Coupon, CouponRedemption


MONEY = Decimal("0.01")
ACTIVE_REDEMPTION_STATUSES = (
    CouponRedemption.Status.RESERVED,
    CouponRedemption.Status.CONSUMED,
)


class CouponValidationError(ValueError):
    """Safe customer-facing coupon rejection."""


def _money(value):
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _customer_key(order):
    if order.client_id:
        return f"client:{order.client_id}"
    email = (order.customer_email or "").strip().casefold()
    if email:
        return f"email:{email}"
    phone = "".join(character for character in (order.customer_phone or "") if character.isdigit())
    return f"phone:{phone}" if phone else ""


def _eligible_subtotal(coupon, product_totals):
    if coupon.scope == Coupon.Scope.ALL_PRODUCTS:
        return sum(product_totals.values(), Decimal("0.00"))

    product_ids = set(product_totals)
    if coupon.scope == Coupon.Scope.PRODUCTS:
        eligible_ids = set(coupon.products.filter(pk__in=product_ids).values_list("pk", flat=True))
    else:
        category_ids = set(coupon.categories.values_list("pk", flat=True))
        eligible_ids = set(
            Product.objects.filter(pk__in=product_ids)
            .filter(Q(section_id__in=category_ids) | Q(categories__id__in=category_ids))
            .values_list("pk", flat=True)
            .distinct()
        )
    return sum((product_totals[product_id] for product_id in eligible_ids), Decimal("0.00"))


def _validate_locked_coupon(coupon, order, customer_key):
    now = timezone.now()
    if not coupon.is_active:
        raise CouponValidationError("This coupon is not available.")
    if coupon.starts_at and coupon.starts_at > now:
        raise CouponValidationError("This coupon is not available yet.")
    if coupon.ends_at and coupon.ends_at <= now:
        raise CouponValidationError("This coupon has expired.")
    if coupon.total_usage_limit is not None and coupon.usage_count >= coupon.total_usage_limit:
        raise CouponValidationError("This coupon has reached its usage limit.")
    if coupon.per_customer_usage_limit is not None:
        if not customer_key:
            raise CouponValidationError("Add an email address or phone number to use this coupon.")
        usage = CouponRedemption.objects.filter(
            coupon=coupon,
            customer_key=customer_key,
            status__in=ACTIVE_REDEMPTION_STATUSES,
        ).count()
        if usage >= coupon.per_customer_usage_limit:
            raise CouponValidationError("This coupon has reached its usage limit for this customer.")


def price_coupon(order, code, product_totals, *, lock=False):
    """Validate and price without consuming usage; order may be unsaved."""
    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        return None, Decimal("0.00")
    try:
        coupons = Coupon.objects.select_for_update() if lock else Coupon.objects.all()
        coupon = coupons.get(
            store=order.store,
            code__iexact=normalized_code,
        )
    except Coupon.DoesNotExist as exc:
        raise CouponValidationError("This coupon is not valid.") from exc

    customer_key = _customer_key(order)
    _validate_locked_coupon(coupon, order, customer_key)
    if order.subtotal < coupon.minimum_subtotal:
        raise CouponValidationError("The order does not meet this coupon's minimum amount.")

    eligible_subtotal = _money(_eligible_subtotal(coupon, product_totals))
    if eligible_subtotal <= 0:
        raise CouponValidationError("This coupon does not apply to the selected products.")
    if coupon.discount_type == Coupon.DiscountType.PERCENTAGE:
        discount = eligible_subtotal * coupon.value / Decimal("100")
        if coupon.maximum_discount is not None:
            discount = min(discount, coupon.maximum_discount)
    else:
        discount = min(coupon.value, eligible_subtotal)
    discount = min(_money(discount), order.subtotal)
    return coupon, discount


@transaction.atomic
def reserve_coupon(order, code, product_totals):
    """Revalidate under lock and reserve one coupon use for an order."""
    coupon, discount = price_coupon(order, code, product_totals, lock=True)
    if coupon is None:
        return discount
    customer_key = _customer_key(order)

    CouponRedemption.objects.create(
        coupon=coupon,
        order=order,
        customer_key=customer_key,
        discount_amount=discount,
        status=CouponRedemption.Status.RESERVED,
    )
    coupon.usage_count += 1
    coupon.save(update_fields=["usage_count", "updated_at"])

    order.applied_coupon_id = coupon.pk
    order.coupon_code = coupon.code
    order.coupon_discount_type = coupon.discount_type
    order.coupon_value = coupon.value
    order.discount_amount = discount
    order.save(update_fields=[
        "applied_coupon_id", "coupon_code", "coupon_discount_type",
        "coupon_value", "discount_amount", "updated_at",
    ])
    return discount


@transaction.atomic
def consume_coupon(order):
    redemption = CouponRedemption.objects.select_for_update().filter(order=order).first()
    if redemption is None or redemption.status != CouponRedemption.Status.RESERVED:
        return
    redemption.status = CouponRedemption.Status.CONSUMED
    redemption.consumed_at = timezone.now()
    redemption.save(update_fields=["status", "consumed_at"])


@transaction.atomic
def release_coupon(order):
    redemption = CouponRedemption.objects.select_for_update().filter(order=order).first()
    if redemption is None or redemption.status == CouponRedemption.Status.RELEASED:
        return
    if redemption.coupon_id:
        coupon = Coupon.objects.select_for_update().get(pk=redemption.coupon_id)
        coupon.usage_count = max(0, coupon.usage_count - 1)
        coupon.save(update_fields=["usage_count", "updated_at"])
    redemption.status = CouponRedemption.Status.RELEASED
    redemption.released_at = timezone.now()
    redemption.save(update_fields=["status", "released_at"])


@transaction.atomic
def reserve_coupon_for_retry(order):
    redemption = CouponRedemption.objects.select_for_update().filter(order=order).first()
    if redemption is None or redemption.status in ACTIVE_REDEMPTION_STATUSES:
        return
    if redemption.coupon_id is None:
        raise CouponValidationError("This coupon is no longer available.")
    coupon = Coupon.objects.select_for_update().get(pk=redemption.coupon_id)
    _validate_locked_coupon(coupon, order, redemption.customer_key)
    coupon.usage_count += 1
    coupon.save(update_fields=["usage_count", "updated_at"])
    redemption.status = CouponRedemption.Status.RESERVED
    redemption.released_at = None
    redemption.save(update_fields=["status", "released_at"])
