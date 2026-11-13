"""Order lifecycle logic, ported from legacy store/payment.py and generalized
to the config-driven catalog (StockVariant keyed by attribute JSON instead of
legacy product+size rows).

Everything is tenant-scoped: the caller must pass the resolved `store`.

Lifecycle:
  pending  →  confirmed  →  shipped  →  delivered
     ↓            ↓
  cancelled    cancelled
"""
import traceback
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from catalog.models import Product, StockVariant
from coupons.services import (
    CouponValidationError as InvalidCouponError,
    consume_coupon,
    price_coupon,
    release_coupon,
    reserve_coupon,
    reserve_coupon_for_retry,
)
from store.models import ClientProfile, Store, StoreConfig, StoreCustomer, StoreMember

from .models import Order, OrderedProduct, OrderStatusHistory, QuantityException


class InvalidOrderItemError(ValueError):
    pass


class InvalidTransitionError(ValueError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class CheckoutPriceChangedError(ValueError):
    pass


# ── Helpers ────────────────────────────────────────────────────────────────

def _unit_price(product, variant):
    """Resolve the selling price while preserving legacy percentage products.

    The commerce model treats ``price`` as the amount charged and
    ``compare_at_price`` as presentation-only. Older products without a
    compare-at price may still use the legacy percentage field.
    """
    from catalog.variant_services import effective_variant_pricing

    pricing = effective_variant_pricing(product, variant)
    base = pricing["selling_price"]
    if pricing["compare_at_price"] is not None:
        return base
    promo = Decimal(str(product.promo or 0))
    return (Decimal(str(base)) * (Decimal("1") - promo / Decimal("100"))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )


def _shipping_amount(config, discounted_merchandise_subtotal):
    shipping = Decimal(str(config.shipping_fee)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not config.free_shipping_enabled:
        return shipping
    if not config.free_shipping_threshold:
        raise InvalidOrderItemError("Free-shipping threshold is not configured.")
    if config.free_shipping_currency != config.currency:
        raise InvalidOrderItemError("Free-shipping threshold currency must match the order currency.")
    threshold = Decimal(str(config.free_shipping_threshold)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Decimal("0.00") if discounted_merchandise_subtotal > threshold else shipping


def _invoicing_enabled(store, config):
    if not config.invoice_enabled:
        return False
    from subscriptions.entitlements import owner_has_store_entitlement
    from subscriptions.policies import INVOICING_KEY

    return owner_has_store_entitlement(store, INVOICING_KEY)


def _tax_totals(config, taxable_merchandise, shipping, *, invoicing_enabled=None):
    """Return the merchant-declared centralized tax snapshot."""
    enabled = config.invoice_enabled if invoicing_enabled is None else invoicing_enabled
    if not enabled or config.invoice_tax_rate is None:
        return Decimal("0.00"), []
    rate = Decimal(str(config.invoice_tax_rate))
    rows = []
    tax_total = Decimal("0.00")
    taxable_merchandise = max(Decimal("0.00"), taxable_merchandise)
    if taxable_merchandise > 0:
        if config.invoice_prices_include_tax:
            tax = (taxable_merchandise * rate / (Decimal("100") + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if rate else Decimal("0.00")
            base = taxable_merchandise - tax
        else:
            base = taxable_merchandise
            tax = (base * rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append({"rate": str(rate), "base": str(base), "tax": str(tax)})
        tax_total += tax
    if shipping:
        if config.invoice_prices_include_tax:
            tax = (shipping * rate / (Decimal("100") + rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if rate else Decimal("0.00")
            base = shipping - tax
        else:
            base = shipping
            tax = (shipping * rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows.append({"rate": str(rate), "base": str(base), "tax": str(tax), "shipping": True})
        tax_total += tax
    return tax_total.quantize(Decimal("0.01")), rows


def _lock_order(order):
    """Lock the durable row while preserving the caller's model instance."""
    locked = Order.objects.select_for_update().get(pk=order.pk)
    for field in Order._meta.concrete_fields:
        setattr(order, field.attname, getattr(locked, field.attname))
    return order


def _release_reservations(order):
    """Release only the stock reservation owned by each order line."""
    lines = OrderedProduct.objects.select_for_update().filter(order=order, stock_deducted=False)
    for op in lines:
        held = op.reserved_quantity
        if held <= 0:
            continue
        try:
            variant = StockVariant.objects.select_for_update().get(
                store=order.store,
                product_id=op.product_id,
                id=op.variant_id,
            )
            variant.reserved = max(0, variant.reserved - held)
            variant.save(update_fields=["reserved"])
        except StockVariant.DoesNotExist:
            pass
        op.reserved_quantity = 0
        op.save(update_fields=["reserved_quantity"])


@transaction.atomic
def reserve_order_for_retry(order):
    """Reacquire the exact quantity priced on a failed online order.

    Failure webhooks release stock. A retry must reserve it again before a new
    checkout is issued; otherwise a later paid webhook could consume stock
    promised to another customer.
    """
    order = _lock_order(order)
    if order.payment_mode != Order.PaymentMode.ONLINE or order.status != Order.Status.PENDING:
        raise InvalidTransitionError("This order cannot be retried.")
    items = list(OrderedProduct.objects.select_for_update().filter(order=order, stock_deducted=False))
    reservations = []
    for item in items:
        try:
            variant = StockVariant.objects.select_for_update().select_related("product").get(
                store=order.store,
                product_id=item.product_id,
                id=item.variant_id,
            )
        except StockVariant.DoesNotExist as exc:
            raise InvalidTransitionError("One or more items are no longer available.") from exc
        if not variant.product.track_inventory:
            reservations.append((item, variant, 0))
            continue
        quantity = max(0, item.fulfilled_quantity - item.reserved_quantity)
        if variant.available_quantity() < quantity:
            raise InvalidTransitionError("One or more items are no longer available.")
        reservations.append((item, variant, quantity))
    for item, variant, quantity in reservations:
        variant.reserved += quantity
        variant.save(update_fields=["reserved"])
        item.reserved_quantity += quantity
        item.save(update_fields=["reserved_quantity"])
    try:
        reserve_coupon_for_retry(order)
    except InvalidCouponError as exc:
        raise InvalidTransitionError("The coupon on this order is no longer available.") from exc
    order.is_paid = Order.IsPaid.PENDING
    order.transaction_id = ""
    order.save(update_fields=["is_paid", "transaction_id", "updated_at"])
    return order


def _award_loyalty_points(client, store, points):
    if not client or points <= 0:
        return
    customer, _ = StoreCustomer.objects.get_or_create(profile=client, store=store)
    StoreCustomer.objects.filter(pk=customer.pk).update(
        loyalty_points=F("loyalty_points") + points,
    )


def _revoke_loyalty_points(client, store, points):
    if not client or points <= 0:
        return
    customer = StoreCustomer.objects.select_for_update().filter(profile=client, store=store).first()
    if customer is None:
        return
    customer.loyalty_points = max(0, customer.loyalty_points - points)
    customer.save(update_fields=["loyalty_points"])


def _record_exception(order, client, product, attributes, shortfall):
    exception_id = QuantityException.objects.create(
        store=order.store,
        client=client,
        order=order,
        product_id=product.id,
        section_key=product.section.key,
        ref=product.ref,
        name=product.name,
        attributes=attributes,
        delta_quantity=shortfall,
    )
    return exception_id


def _resolve_client(request, store):
    """Link the order to the caller's ClientProfile, if authenticated.
    Never raises — guest checkout must keep working."""
    try:
        from rest_framework.request import Request

        if not isinstance(request, Request):
            return None
        user = request.user if request.user and request.user.is_authenticated else None
        if not user:
            return None
        return ClientProfile.objects.filter(
            user=user,
            store_customers__store=store,
        ).first()
    except Exception:
        return None


def _record_transition(order, from_status, to_status, changed_by=None, notes=""):
    """Append a row to the order's audit trail."""
    OrderStatusHistory.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        notes=notes,
    )


def _send_notification(order, event):
    """Best-effort email — never breaks the request."""
    try:
        from notifications.service import (
            send_order_cancelled,
            send_order_confirmation,
            send_order_delivered,
            send_order_received,
            send_order_shipped,
        )

        dispatch = {
            "received": send_order_received,
            "confirmed": send_order_confirmation,
            "shipped": send_order_shipped,
            "delivered": send_order_delivered,
            "cancelled": send_order_cancelled,
        }
        fn = dispatch.get(event)
        if fn:
            fn(order)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _notify_on_commit(order, event):
    """Never announce a transition before its database transaction commits."""
    order_id = order.pk

    def dispatch():
        persisted = Order.objects.filter(pk=order_id).first()
        if persisted is not None:
            _send_notification(persisted, event)

    transaction.on_commit(dispatch)


# ── Order creation ─────────────────────────────────────────────────────────

def _checkout_item(store, item, *, lock=False):
    try:
        product = Product.objects.select_related("section").get(
            store=store, id=item["product_id"], is_active=True,
        )
    except Product.DoesNotExist as exc:
        raise InvalidOrderItemError("One or more requested products are unavailable.") from exc
    variants = StockVariant.objects.select_for_update() if lock else StockVariant.objects.all()
    variants = variants.filter(store=store, product=product, is_active=True)
    if item.get("variant_id") is not None:
        variant = variants.filter(pk=item["variant_id"]).first()
    else:
        if product.uses_variants:
            raise InvalidOrderItemError("Please choose an option for this product.")
        variant = variants.filter(attributes={}).first()
    if variant is None:
        raise InvalidOrderItemError("One or more requested products are unavailable.")
    if product.uses_variants and not variant.attributes:
        raise InvalidOrderItemError("Please choose an available option for this product.")
    if not product.uses_variants and variant.attributes:
        raise InvalidOrderItemError("This product's inventory configuration is inconsistent.")
    return product, variant


def quote_order(store, customer, items, request=None, coupon_code=""):
    """Read-only pricing: no order, stock reservation, redemption or email."""
    config = StoreConfig.objects.get(store=store)
    product_totals = {}
    quantities = {}
    for item in items:
        product, variant = _checkout_item(store, item)
        quantity = int(item["quantity"])
        quantities[variant.pk] = quantities.get(variant.pk, 0) + quantity
        if product.track_inventory and quantities[variant.pk] > variant.available_quantity():
            raise InvalidOrderItemError("The requested quantity is no longer available.")
        product_totals[product.pk] = (
            product_totals.get(product.pk, Decimal("0.00")) + _unit_price(product, variant) * quantity
        )
    subtotal = sum(product_totals.values(), Decimal("0.00"))
    snapshot = Order(
        store=store, client=_resolve_client(request, store), subtotal=subtotal,
        customer_email=customer.get("email", ""), customer_phone=customer.get("phone", ""),
    )
    coupon, discount = price_coupon(snapshot, coupon_code, product_totals)
    shipping = _shipping_amount(config, subtotal - discount)
    invoicing_enabled = _invoicing_enabled(store, config)
    tax, tax_breakdown = _tax_totals(
        config, subtotal - discount, shipping, invoicing_enabled=invoicing_enabled,
    )
    total = subtotal - discount + shipping + (tax if not config.invoice_prices_include_tax else Decimal("0.00"))
    return {
        "subtotal": subtotal, "discount": discount, "shipping": shipping,
        "tax": tax, "tax_breakdown": tax_breakdown, "total": total, "currency": config.currency,
        "coupon_code": coupon.code if coupon else "",
    }


@transaction.atomic
def create_order(store, customer, items, payment_mode=Order.PaymentMode.ONLINE,
                 request=None, notes="", marketing_consent=False, marketing_tracking_consent=False,
                 marketing_tracking_id="", coupon_code="",
                 idempotency_key="", checkout_fingerprint="", expected_totals=None):
    """Create a pending order from a customer snapshot and reserve its stock.

    Both online and cash-on-delivery orders start pending. Stock is deducted only
    after the order is confirmed by the payment flow or by a merchant.

    Shortfalls are recorded as QuantityExceptions and the order is flagged.
    """
    if payment_mode not in (Order.PaymentMode.ONLINE, Order.PaymentMode.COD):
        raise ValueError("payment_mode must be 'online' or 'cod'")

    idempotency_key = (idempotency_key or "").strip()
    if idempotency_key:
        # Lock the tenant row so two simultaneous first uses of the same key
        # cannot both pass an absent-row check before the unique constraint.
        Store.objects.select_for_update().only("pk").get(pk=store.pk)
        existing = Order.objects.filter(store=store, idempotency_key=idempotency_key).first()
        if existing is not None:
            if existing.checkout_fingerprint != checkout_fingerprint:
                raise IdempotencyConflictError("This checkout request has already been used.")
            return existing

    config = StoreConfig.objects.get(store=store)
    invoicing_enabled = _invoicing_enabled(store, config)
    is_cod = payment_mode == Order.PaymentMode.COD

    tracking_hash = ""
    if marketing_tracking_consent and marketing_tracking_id:
        from commerce_analytics.privacy import tracking_visitor_hash
        tracking_hash = tracking_visitor_hash(store, marketing_tracking_id)

    order = Order.objects.create(
        store=store,
        client=_resolve_client(request, store),
        payment_mode=payment_mode,
        status=Order.Status.PENDING,
        is_paid=Order.IsPaid.COD if is_cod else Order.IsPaid.PENDING,
        currency=config.currency,
        tax_rate=config.invoice_tax_rate if invoicing_enabled else None,
        prices_include_tax=config.invoice_prices_include_tax,
        customer_name=customer.get("name", ""),
        customer_email=customer.get("email", ""),
        customer_phone=customer.get("phone", ""),
        customer_city=customer.get("city", ""),
        customer_address=customer.get("address", ""),
        customer_notes=notes,
        marketing_consent=marketing_consent,
        marketing_tracking_consent=marketing_tracking_consent,
        marketing_tracking_visitor_hash=tracking_hash,
        idempotency_key=idempotency_key,
        checkout_fingerprint=checkout_fingerprint if idempotency_key else "",
    )

    subtotal = Decimal("0.00")
    total_fulfilled = 0
    product_totals = {}

    for item in items:
        product, variant = _checkout_item(store, item, lock=True)

        requested_qty = int(item["quantity"])
        if requested_qty < 1:
            continue

        available_qty = requested_qty if not product.track_inventory else variant.available_quantity()
        can_fulfill = available_qty >= requested_qty
        if expected_totals is not None and not can_fulfill:
            raise CheckoutPriceChangedError("Availability changed. Review your order again.")
        fulfilled_qty = min(requested_qty, available_qty)

        reserved_qty = fulfilled_qty if product.track_inventory else 0
        if reserved_qty:
            variant.reserved += reserved_qty
            variant.save(update_fields=["reserved"])

        unit_price = _unit_price(product, variant)
        line_total = unit_price * fulfilled_qty
        subtotal += line_total
        product_totals[product.id] = product_totals.get(product.id, Decimal("0.00")) + line_total
        total_fulfilled += fulfilled_qty

        exception_id = None
        if not can_fulfill:
            shortfall = requested_qty - available_qty
            exception_id = _record_exception(order, order.client, product, variant.attributes, shortfall)
            order.exception = True
            order.save(update_fields=["exception"])

        OrderedProduct.objects.create(
            order=order,
            store=store,
            product_id=product.id,
            variant_id=variant.id if variant is not None else 0,
            section_key=product.section.key,
            ref=product.ref,
            name=product.name,
            attributes=variant.attributes if variant is not None else {},
            quantity=requested_qty,
            fulfilled_quantity=fulfilled_qty,
            reserved_quantity=reserved_qty,
            price=unit_price,
            tax_rate=config.invoice_tax_rate if invoicing_enabled else None,
            available=can_fulfill,
            exception_id=exception_id.id if exception_id else None,
            stock_deducted=False,
        )

    if total_fulfilled == 0:
        raise InvalidOrderItemError("The requested products are no longer available.")

    order.subtotal = subtotal.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    order.save(update_fields=["subtotal", "updated_at"])
    discount_amount = reserve_coupon(order, coupon_code, product_totals)
    shipping_amount = _shipping_amount(config, order.subtotal - discount_amount)
    tax_amount, tax_breakdown = _tax_totals(
        config,
        order.subtotal - discount_amount,
        shipping_amount,
        invoicing_enabled=invoicing_enabled,
    )
    order.shipping_amount = shipping_amount
    order.tax_amount = tax_amount
    order.tax_breakdown = tax_breakdown
    order.amount = (order.subtotal - discount_amount + shipping_amount + (tax_amount if not config.invoice_prices_include_tax else Decimal("0.00"))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    if expected_totals is not None and expected_totals != {
        "subtotal": order.subtotal, "discount": discount_amount,
        "shipping": shipping_amount, "tax": tax_amount, "total": order.amount, "currency": order.currency,
    }:
        # Raising inside the transaction also rolls back stock and coupon use.
        raise CheckoutPriceChangedError("Prices changed. Review your order again.")
    order.save(update_fields=["subtotal", "shipping_amount", "tax_amount", "tax_breakdown", "amount"])

    if marketing_consent and order.customer_email:
        from marketing.models import Subscriber

        subscriber, _ = Subscriber.objects.get_or_create(
            store=store,
            email=order.customer_email.lower().strip(),
            defaults={"status": Subscriber.Status.ACTIVE},
        )
        if subscriber.status != Subscriber.Status.ACTIVE:
            subscriber.status = Subscriber.Status.ACTIVE
            subscriber.unsubscribed_at = None
            subscriber.save(update_fields=["status", "unsubscribed_at"])

    if is_cod:
        _notify_on_commit(order, "received")
    return order


# ── State transitions ──────────────────────────────────────────────────────

@transaction.atomic
def confirm_order(order, changed_by=None, notes="", acknowledged_item_ids=None, invoice_locale=None):
    """Convert reservations → real deductions. Idempotent (guarded per item).
    pending → confirmed. Awards loyalty points for quantity actually deducted."""
    order = _lock_order(order)
    if order.status != Order.Status.PENDING:
        raise InvalidTransitionError(
            f"Cannot confirm order in '{order.status}' status."
        )
    if order.payment_mode == Order.PaymentMode.ONLINE and order.is_paid != Order.IsPaid.CONFIRMED:
        raise InvalidTransitionError("Payment has not been received for this order.")

    if changed_by is not None:
        expected_ids = set(order.items.values_list("id", flat=True))
        acknowledged_ids = set(acknowledged_item_ids or [])
        if acknowledged_ids != expected_ids:
            raise InvalidTransitionError("Review and acknowledge every order item before confirming.")

    lines = list(OrderedProduct.objects.select_for_update().filter(order=order))
    allocations = []
    for op in lines:
        if op.stock_deducted:
            continue
        try:
            variant = StockVariant.objects.select_for_update().select_related("product").get(
                store=order.store, product_id=op.product_id, id=op.variant_id,
            )
        except StockVariant.DoesNotExist as exc:
            raise InvalidTransitionError("One or more order items no longer have inventory.") from exc
        if variant.product.track_inventory:
            if op.reserved_quantity != op.fulfilled_quantity:
                raise InvalidTransitionError("Inventory is no longer reserved for every order item.")
            if variant.reserved < op.reserved_quantity or variant.quantity < op.reserved_quantity:
                raise InvalidTransitionError("Reserved inventory is inconsistent. Review this order before confirming.")
        elif op.reserved_quantity != 0:
            raise InvalidTransitionError("Untracked inventory contains an unexpected reservation.")
        allocations.append((op, variant))

    old_status = order.status
    now = timezone.now()
    total_loyalty = 0

    # Deduct reserved stock
    for op, variant in allocations:
        deduct = op.reserved_quantity
        if variant.product.track_inventory:
            variant.reserved -= deduct
            variant.quantity -= deduct
            variant.save(update_fields=["quantity", "reserved"])

        op.reserved_quantity = 0
        op.stock_deducted = True
        op.save(update_fields=["reserved_quantity", "stock_deducted"])

        product = Product.objects.filter(pk=op.product_id).first()
        if product:
            total_loyalty += product.loyalty_points * op.fulfilled_quantity

    # Update order
    consume_coupon(order)
    order.status = Order.Status.CONFIRMED
    order.is_paid = Order.IsPaid.CONFIRMED if order.payment_mode == Order.PaymentMode.ONLINE else Order.IsPaid.COD
    order.confirmed_at = now
    order.invoice_locale = invoice_locale or order.invoice_locale or StoreConfig.objects.get(store=order.store).default_locale
    if changed_by is not None:
        order.items_acknowledged_at = now
        order.items_acknowledged_by = changed_by
    order.loyalty_points_awarded = total_loyalty if StoreConfig.objects.get(store=order.store).loyalty_enabled else 0
    order.save(update_fields=[
        "status", "is_paid", "confirmed_at", "items_acknowledged_at",
        "items_acknowledged_by", "loyalty_points_awarded", "invoice_locale", "updated_at",
    ])

    # Award loyalty
    _award_loyalty_points(order.client, order.store, order.loyalty_points_awarded)

    _record_transition(order, old_status, Order.Status.CONFIRMED, changed_by, notes)
    if order.marketing_tracking_consent:
        from commerce_analytics.meta import queue_order_lifecycle_event
        queue_order_lifecycle_event(order, "OrderConfirmed")
        if order.payment_mode == Order.PaymentMode.ONLINE:
            queue_order_lifecycle_event(order, "PaymentCollected")
    _notify_on_commit(order, "confirmed")
    return order


@transaction.atomic
def ship_order(order, changed_by=None, tracking_number="", carrier="",
               delivery_person_id=None, notes=""):
    """confirmed → shipped. Records tracking and optional delivery person."""
    order = _lock_order(order)
    tracking_number = tracking_number.strip()
    carrier = carrier.strip()
    if not tracking_number or not carrier:
        raise InvalidTransitionError("Carrier and tracking number are required before shipping.")
    if order.status != Order.Status.CONFIRMED:
        raise InvalidTransitionError(
            f"Cannot ship order in '{order.status}' status."
        )

    old_status = order.status
    now = timezone.now()

    order.status = Order.Status.SHIPPED
    order.shipped_at = now
    order.tracking_number = tracking_number
    order.carrier = carrier

    if delivery_person_id is not None:
        try:
            dm = StoreMember.objects.get(
                pk=delivery_person_id,
                store=order.store,
                role=StoreMember.Roles.DELIVERY,
            )
            order.delivery_person = dm
        except StoreMember.DoesNotExist as exc:
            raise InvalidTransitionError("Choose a valid delivery team member.") from exc

    order.save(update_fields=[
        "status", "shipped_at", "tracking_number", "carrier",
        "delivery_person", "updated_at",
    ])

    _record_transition(order, old_status, Order.Status.SHIPPED, changed_by, notes)
    _notify_on_commit(order, "shipped")
    return order


@transaction.atomic
def deliver_order(order, changed_by=None, notes=""):
    """shipped → delivered."""
    order = _lock_order(order)
    if order.status != Order.Status.SHIPPED:
        raise InvalidTransitionError(
            f"Cannot mark order as delivered from '{order.status}' status."
        )

    old_status = order.status
    now = timezone.now()

    order.status = Order.Status.DELIVERED
    order.delivered_at = now
    if order.payment_mode == Order.PaymentMode.COD:
        order.is_paid = Order.IsPaid.CONFIRMED
    order.save(update_fields=["status", "is_paid", "delivered_at", "updated_at"])

    _record_transition(order, old_status, Order.Status.DELIVERED, changed_by, notes)
    if order.marketing_tracking_consent:
        from commerce_analytics.meta import queue_order_lifecycle_event
        queue_order_lifecycle_event(order, "OrderDelivered")
        if order.payment_mode == Order.PaymentMode.COD:
            queue_order_lifecycle_event(order, "Purchase")
            queue_order_lifecycle_event(order, "PaymentCollected")
    _notify_on_commit(order, "delivered")
    from .invoices import schedule_invoice
    schedule_invoice(order.pk)
    return order


@transaction.atomic
def cancel_order(order, cancelled_by=Order.CancelledBy.MERCHANT, reason="",
                 changed_by=None):
    """Cancel an active order. Releases reservations if stock not yet deducted."""
    order = _lock_order(order)
    if order.status in (Order.Status.SHIPPED, Order.Status.DELIVERED, Order.Status.CANCELLED):
        raise InvalidTransitionError(
            f"Cannot cancel order in '{order.status}' status."
        )

    old_status = order.status
    now = timezone.now()

    if order.payment_mode == Order.PaymentMode.ONLINE and order.is_paid == Order.IsPaid.CONFIRMED:
        raise InvalidTransitionError("Paid online orders require a refund workflow before cancellation.")

    if order.status == Order.Status.CONFIRMED:
        for op in OrderedProduct.objects.select_for_update().filter(order=order, stock_deducted=True):
            try:
                variant = StockVariant.objects.select_for_update().select_related("product").get(
                    store=order.store, product_id=op.product_id, id=op.variant_id,
                )
            except StockVariant.DoesNotExist as exc:
                raise InvalidTransitionError("Cannot restore inventory for one or more order items.") from exc
            if variant.product.track_inventory:
                variant.quantity += op.fulfilled_quantity
                variant.save(update_fields=["quantity"])
            op.stock_deducted = False
            op.save(update_fields=["stock_deducted"])
        _revoke_loyalty_points(order.client, order.store, order.loyalty_points_awarded)
        order.loyalty_points_awarded = 0
    else:
        _release_reservations(order)
    release_coupon(order)

    order.status = Order.Status.CANCELLED
    order.cancelled_by = cancelled_by
    order.cancel_reason = reason
    order.cancelled_at = now
    order.save(update_fields=[
        "status", "cancelled_by", "cancel_reason", "cancelled_at",
        "loyalty_points_awarded", "updated_at",
    ])

    _record_transition(order, old_status, Order.Status.CANCELLED, changed_by, reason)
    _notify_on_commit(order, "cancelled")
    return order


@transaction.atomic
def fail_order(order):
    """Mark the order failed and release any reservations still held.
    Used by payment webhooks (not a normal state transition)."""
    order = _lock_order(order)
    if order.is_paid == Order.IsPaid.FAILED:
        return order
    if order.status != Order.Status.PENDING or order.is_paid in (Order.IsPaid.CONFIRMED, Order.IsPaid.COD):
        raise InvalidTransitionError("This order can no longer be marked as failed.")
    _release_reservations(order)
    release_coupon(order)
    order.is_paid = Order.IsPaid.FAILED
    order.save(update_fields=["is_paid", "updated_at"])
    return order


@transaction.atomic
def update_notes(order, merchant_notes=None):
    """Update internal merchant notes on an order."""
    order = _lock_order(order)
    if merchant_notes is not None:
        order.merchant_notes = merchant_notes
        order.save(update_fields=["merchant_notes", "updated_at"])
    return order
