"""End-to-end tests: StoreConfig + tenant-scoped order lifecycle.

Run with: ./venv/bin/python manage.py test orders --keepdb
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.conf import settings
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.cookies import access_cookie_name
from accounts.tokens import CUSTOMER_SCOPE, issue_scoped_tokens

from catalog.models import AttributeDef, CatalogSection, Product, StockVariant
from coupons.models import Coupon, CouponRedemption
from store.models import ClientProfile, Store, StoreConfig, StoreCustomer, StoreMember

from .models import Order, OrderedProduct, QuantityException
from .services import (
    InvalidCouponError,
    InvalidOrderItemError,
    InvalidTransitionError,
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
    fail_order,
    reserve_order_for_retry,
    ship_order,
    update_notes,
)


def make_store(slug="hygiene", name="Hygiene Store"):
    store, _ = Store.objects.get_or_create(slug=slug, defaults={"name": name})
    StoreConfig.objects.get_or_create(store=store)
    section, _ = CatalogSection.objects.get_or_create(
        store=store, key="soap", defaults={"label": "Soap", "sort_order": 0}
    )
    AttributeDef.objects.get_or_create(
        store=store, section=section, key="size", defaults={"type": "select", "options": ["small", "large"], "required": True}
    )
    return store, section


def make_product(store, section, ref="soap-1", quantity=5, reserved=0, price=100):
    product = Product.objects.create(
        store=store, section=section, ref=ref, name=f"{ref} name",
        price=price, promo=0, loyalty_points=10, attributes={"size": "small"},
        uses_variants=True,
    )
    variant = StockVariant.objects.create(
        store=store, product=product, attributes={"size": "small"},
        quantity=quantity, reserved=reserved, price=price,
    )
    return product, variant


def item(product, variant, qty):
    return {"product_id": product.id, "variant_id": variant.id, "quantity": qty}


class StoreConfigTests(TestCase):
    def test_config_auto_created_on_store_create(self):
        store = Store.objects.create(slug="auto", name="Auto")
        self.assertTrue(hasattr(store, "config"))
        self.assertEqual(store.config.currency, "MAD")
        self.assertEqual(store.config.shipping_fee, 0)
        self.assertTrue(store.config.cod_enabled)

    def test_merchant_can_update_config(self):
        user = get_user_model().objects.create_user(email="owner@example.com", password="pw")
        store, _ = make_store(slug="cfg")
        StoreMember.objects.create(user=user, store=store, role=StoreMember.Roles.OWNER)
        client = APIClient()
        client.force_authenticate(user)

        res = client.patch(
            reverse("store-config", args=[store.id]),
            {"currency": "EUR", "shipping_fee": 15, "brand_color": "#123ABC"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        store.config.refresh_from_db()
        self.assertEqual(store.config.currency, "EUR")
        self.assertEqual(store.config.shipping_fee, 15)

    def test_bad_hex_color_rejected(self):
        user = get_user_model().objects.create_user(email="owner2@example.com", password="pw")
        store, _ = make_store(slug="cfg2")
        StoreMember.objects.create(user=user, store=store, role=StoreMember.Roles.OWNER)
        client = APIClient()
        client.force_authenticate(user)
        res = client.patch(reverse("store-config", args=[store.id]), {"brand_color": "red"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_non_member_cannot_touch_config(self):
        user = get_user_model().objects.create_user(email="outsider@example.com", password="pw")
        store, _ = make_store(slug="cfg3")
        client = APIClient()
        client.force_authenticate(user)
        res = client.get(reverse("store-config", args=[store.id]))
        self.assertEqual(res.status_code, 404)


class OrderServiceTests(TestCase):
    def setUp(self):
        self.store, self.section = make_store()
        StoreConfig.objects.filter(store=self.store).update(shipping_fee=10, currency="MAD")
        self.product, self.variant = make_product(self.store, self.section)

    def test_online_order_reserves_stock(self):
        order = create_order(
            self.store,
            customer={"name": "Amina", "email": "amina@mail.com"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)   # not deducted yet
        self.assertEqual(self.variant.reserved, 2)   # reserved
        self.assertEqual(order.is_paid, Order.IsPaid.PENDING)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.amount, 2 * 100 + 10)  # 2×price + shipping
        self.assertEqual(order.subtotal, 200)
        self.assertEqual(order.shipping_amount, 10)
        op = order.items.get()
        self.assertFalse(op.stock_deducted)
        self.assertEqual(op.price, 100)
        self.assertEqual(op.fulfilled_quantity, 2)
        self.assertEqual(op.reserved_quantity, 2)

    def test_online_order_with_promo_prices(self):
        self.product.promo = 20
        self.product.save()
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        op = order.items.get()
        self.assertEqual(op.price, 80.0)  # 100 × (1 - 0.20)

    def test_simple_product_resolves_internal_inventory_without_variant_id(self):
        product = Product.objects.create(
            store=self.store,
            section=self.section,
            ref="SIMPLE-1",
            name="Simple product",
            price="40.00",
            uses_variants=False,
        )
        variant = StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={},
            quantity=4,
        )

        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[{"product_id": product.id, "quantity": 2}],
            payment_mode=Order.PaymentMode.COD,
        )

        self.assertEqual(order.items.get().variant_id, variant.id)
        variant.refresh_from_db()
        self.assertEqual(variant.reserved, 2)

    def test_configurable_product_still_requires_an_explicit_variant(self):
        with self.assertRaisesRegex(InvalidOrderItemError, "choose an option"):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[{"product_id": self.product.id, "quantity": 1}],
                payment_mode=Order.PaymentMode.COD,
            )

    def test_percentage_coupon_is_scoped_priced_and_snapshotted(self):
        coupon = Coupon.objects.create(
            store=self.store,
            code="SAVE10",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value="10.00",
            scope=Coupon.Scope.CATEGORIES,
            maximum_discount="15.00",
        )
        coupon.categories.add(self.section)

        order = create_order(
            self.store,
            customer={"name": "Amina", "email": "amina@example.test"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=Order.PaymentMode.COD,
            coupon_code="save10",
        )

        self.assertEqual(order.subtotal, 200)
        self.assertEqual(order.discount_amount, 15)
        self.assertEqual(order.shipping_amount, 10)
        self.assertEqual(order.amount, 195)
        self.assertEqual(order.coupon_code, "SAVE10")
        self.assertEqual(order.coupon_discount_type, Coupon.DiscountType.PERCENTAGE)
        self.assertEqual(order.coupon_value, 10)
        coupon.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)
        self.assertEqual(order.coupon_redemption.status, CouponRedemption.Status.RESERVED)

        confirm_order(order)
        order.coupon_redemption.refresh_from_db()
        self.assertEqual(order.coupon_redemption.status, CouponRedemption.Status.CONSUMED)

        cancel_order(order, reason="Customer changed the order")
        coupon.refresh_from_db()
        order.coupon_redemption.refresh_from_db()
        self.assertEqual(coupon.usage_count, 0)
        self.assertEqual(order.coupon_redemption.status, CouponRedemption.Status.RELEASED)

    def test_invalid_coupon_rolls_back_order_and_stock_reservation(self):
        with self.assertRaises(InvalidCouponError):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[item(self.product, self.variant, 1)],
                payment_mode=Order.PaymentMode.ONLINE,
                coupon_code="UNKNOWN",
            )
        self.assertEqual(Order.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)

    def test_failed_payment_releases_coupon_and_retry_reserves_it_again(self):
        coupon = Coupon.objects.create(
            store=self.store,
            code="ONLYONE",
            discount_type=Coupon.DiscountType.FIXED,
            value="20.00",
            total_usage_limit=1,
        )
        order = create_order(
            self.store,
            customer={"name": "Amina", "email": "amina@example.test"},
            items=[item(self.product, self.variant, 1)],
            coupon_code="ONLYONE",
        )

        fail_order(order)
        coupon.refresh_from_db()
        order.coupon_redemption.refresh_from_db()
        self.assertEqual(coupon.usage_count, 0)
        self.assertEqual(order.coupon_redemption.status, CouponRedemption.Status.RELEASED)

        reserve_order_for_retry(order)
        coupon.refresh_from_db()
        order.coupon_redemption.refresh_from_db()
        self.assertEqual(coupon.usage_count, 1)
        self.assertEqual(order.coupon_redemption.status, CouponRedemption.Status.RESERVED)
        self.assertEqual(order.amount, 90)

    def test_per_customer_coupon_limit_counts_reserved_and_consumed_orders(self):
        Coupon.objects.create(
            store=self.store,
            code="WELCOME",
            discount_type=Coupon.DiscountType.FIXED,
            value="10.00",
            per_customer_usage_limit=1,
        )
        create_order(
            self.store,
            customer={"name": "Amina", "email": "AMINA@example.test"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.COD,
            coupon_code="WELCOME",
        )
        with self.assertRaises(InvalidCouponError):
            create_order(
                self.store,
                customer={"name": "Amina", "email": "amina@example.test"},
                items=[item(self.product, self.variant, 1)],
                payment_mode=Order.PaymentMode.COD,
                coupon_code="WELCOME",
            )

    def test_cod_order_starts_pending_and_reserves_stock(self):
        order = create_order(
            self.store,
            customer={"name": "Amina", "phone": "0600000000"},
            items=[item(self.product, self.variant, 3)],
            payment_mode=Order.PaymentMode.COD,
        )
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)
        self.assertEqual(self.variant.reserved, 3)
        self.assertEqual(order.is_paid, Order.IsPaid.COD)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertIsNone(order.confirmed_at)
        self.assertFalse(order.items.get().stock_deducted)

        confirm_order(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 2)
        self.assertEqual(self.variant.reserved, 0)
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertTrue(order.items.get().stock_deducted)

    def test_checkout_marketing_consent_adds_subscriber(self):
        from marketing.models import Subscriber

        order = create_order(
            self.store,
            customer={"name": "Amina", "email": "AMINA@example.com"},
            items=[item(self.product, self.variant, 1)],
            marketing_consent=True,
        )
        self.assertTrue(order.marketing_consent)
        self.assertTrue(Subscriber.objects.filter(
            store=self.store,
            email="amina@example.com",
            status=Subscriber.Status.ACTIVE,
        ).exists())

    def test_checkout_without_marketing_consent_does_not_subscribe(self):
        from marketing.models import Subscriber

        create_order(
            self.store,
            customer={"name": "Amina", "email": "amina@example.com"},
            items=[item(self.product, self.variant, 1)],
        )
        self.assertFalse(Subscriber.objects.filter(store=self.store).exists())

    def test_shortfall_records_exception(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 7)],  # only 5 available
            payment_mode=Order.PaymentMode.ONLINE,
        )
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 5)
        self.assertTrue(order.exception)
        exc = QuantityException.objects.get(order=order)
        self.assertEqual(exc.delta_quantity, 2)
        op = order.items.get()
        self.assertFalse(op.available)
        self.assertEqual(op.exception_id, exc.id)
        self.assertEqual(op.quantity, 7)
        self.assertEqual(op.fulfilled_quantity, 5)
        self.assertEqual(op.reserved_quantity, 5)
        self.assertEqual(order.subtotal, 500)

    def test_completely_unavailable_order_is_rejected_without_shipping_only_charge(self):
        from .services import InvalidOrderItemError

        self.variant.quantity = 0
        self.variant.save(update_fields=["quantity"])
        with self.assertRaises(InvalidOrderItemError):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[item(self.product, self.variant, 1)],
                payment_mode=Order.PaymentMode.COD,
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_failure_releases_only_the_reservation_owned_by_that_order(self):
        first = create_order(
            self.store, customer={"name": "First"},
            items=[item(self.product, self.variant, 2)],
        )
        second = create_order(
            self.store, customer={"name": "Second"},
            items=[item(self.product, self.variant, 2)],
        )
        fail_order(first)

        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 2)
        self.assertEqual(first.items.get().reserved_quantity, 0)
        self.assertEqual(second.items.get().reserved_quantity, 2)

        second.is_paid = Order.IsPaid.CONFIRMED
        second.save(update_fields=["is_paid"])
        confirm_order(second)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 3)
        self.assertEqual(self.variant.reserved, 0)

    def test_confirm_order_deducts_and_rejects_double_confirm(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["is_paid"])
        confirm_order(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 3)
        self.assertEqual(self.variant.reserved, 0)
        self.assertEqual(order.is_paid, Order.IsPaid.CONFIRMED)
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertIsNotNone(order.confirmed_at)

        # second confirm must raise
        with self.assertRaises(InvalidTransitionError):
            confirm_order(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 3)

    def test_transition_notification_waits_for_database_commit(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.COD,
        )

        with patch("orders.services._send_notification") as send:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                confirm_order(order)
            send.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1], "confirmed")

    def test_confirm_awards_loyalty_points(self):
        profile_user = get_user_model().objects.create_user(email="client@example.com", password="pw")
        profile = ClientProfile.objects.create(user=profile_user)
        profile.stores.add(self.store)

        order = create_order(
            self.store,
            customer={"name": "Client"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=Order.PaymentMode.ONLINE,
            request=None,
        )
        order.client = profile
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["client", "is_paid"])
        confirm_order(order)
        customer = StoreCustomer.objects.get(profile=profile, store=self.store)
        self.assertEqual(customer.loyalty_points, 10 * 2)  # loyalty_points × fulfilled qty

    def test_partial_order_awards_only_fulfilled_loyalty_and_cancel_reverses_it(self):
        profile_user = get_user_model().objects.create_user(email="partial@example.com", password="pw")
        profile = ClientProfile.objects.create(user=profile_user)
        profile.stores.add(self.store)
        order = create_order(
            self.store,
            customer={"name": "Partial"},
            items=[item(self.product, self.variant, 7)],
            payment_mode=Order.PaymentMode.COD,
        )
        order.client = profile
        order.save(update_fields=["client"])

        confirm_order(order)
        order.refresh_from_db()
        customer = StoreCustomer.objects.get(profile=profile, store=self.store)
        self.assertEqual(order.loyalty_points_awarded, 50)
        self.assertEqual(customer.loyalty_points, 50)

        cancel_order(order, reason="Customer changed the order")
        order.refresh_from_db()
        customer.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(order.loyalty_points_awarded, 0)
        self.assertEqual(customer.loyalty_points, 0)
        self.assertEqual(self.variant.quantity, 5)

    def test_fail_order_releases_reservations(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 4)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        fail_order(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)
        self.assertEqual(self.variant.reserved, 0)
        self.assertEqual(order.is_paid, Order.IsPaid.FAILED)

    def test_unknown_product_item_rejects_the_order(self):
        from .services import InvalidOrderItemError

        with self.assertRaises(InvalidOrderItemError):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[{"product_id": 9999, "variant_id": 9999, "quantity": 1}],
                payment_mode=Order.PaymentMode.ONLINE,
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_product_without_inventory_is_not_treated_as_unlimited_stock(self):
        from .services import InvalidOrderItemError

        unstocked = Product.objects.create(
            store=self.store,
            section=self.section,
            ref="unstocked",
            name="Unstocked product",
            price=50,
            attributes={"size": "small"},
        )
        with self.assertRaises(InvalidOrderItemError):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[{"product_id": unstocked.id, "variant_id": None, "quantity": 1}],
                payment_mode=Order.PaymentMode.ONLINE,
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_inactive_product_cannot_be_ordered_by_id(self):
        from .services import InvalidOrderItemError

        self.product.is_active = False
        self.product.save(update_fields=["is_active"])
        with self.assertRaises(InvalidOrderItemError):
            create_order(
                self.store,
                customer={"name": "Amina"},
                items=[item(self.product, self.variant, 1)],
                payment_mode=Order.PaymentMode.ONLINE,
            )
        self.assertEqual(Order.objects.count(), 0)

    def test_untracked_product_can_be_ordered_without_depleting_inventory(self):
        self.product.track_inventory = False
        self.product.save(update_fields=["track_inventory"])
        self.variant.quantity = 0
        self.variant.reserved = 0
        self.variant.save(update_fields=["quantity", "reserved"])

        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 3)],
            payment_mode=Order.PaymentMode.COD,
        )
        line = order.items.get()
        self.assertEqual(line.fulfilled_quantity, 3)
        self.assertEqual(line.reserved_quantity, 0)
        confirm_order(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 0)
        self.assertEqual(self.variant.reserved, 0)


class OrderApiTests(TestCase):
    def setUp(self):
        self.store, self.section = make_store()
        StoreConfig.objects.filter(store=self.store).update(shipping_fee=0)
        self.product, self.variant = make_product(self.store, self.section)
        self.client = APIClient()

    def _post(self, path, payload, **kw):
        return self.client.post(path, payload, format="json", HTTP_X_STORE_SLUG=self.store.slug, **kw)

    def test_create_online_order_via_api(self):
        res = self._post(
            reverse("order-create"),
            {"payment_mode": "online", "customer": {"name": "Amina", "email": "a@b.c"},
             "items": [item(self.product, self.variant, 2)]},
        )
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data
        self.assertEqual(data["is_paid"], "pending")
        self.assertEqual(data["amount"], "200.00")
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("customer_email", data)
        self.assertNotIn("merchant_notes", data)
        self.assertEqual(data["items"][0]["fulfilled_quantity"], 2)
        self.assertNotIn("reserved_quantity", data["items"][0])
        self.assertNotIn("stock_deducted", data["items"][0])
        self.assertNotIn("exception_id", data["items"][0])
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 2)

    def test_create_cod_order_via_api(self):
        res = self._post(
            reverse("order-create"),
            {"payment_mode": "cod", "customer": {"name": "Amina"},
             "items": [item(self.product, self.variant, 2)]},
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["is_paid"], "cod")
        self.assertEqual(res.data["status"], "pending")
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)
        self.assertEqual(self.variant.reserved, 2)

    def test_disabled_cod_is_rejected_before_an_order_is_created(self):
        self.store.config.cod_enabled = False
        self.store.config.save(update_fields=["cod_enabled"])

        res = self._post(
            reverse("order-create"),
            {"payment_mode": "cod", "customer": {"name": "Amina"},
             "items": [item(self.product, self.variant, 1)]},
        )

        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("payment_mode", res.data)
        self.assertFalse(Order.objects.exists())

    def test_checkout_idempotency_replays_the_same_coupon_order_once(self):
        coupon = Coupon.objects.create(
            store=self.store,
            code="REPLAY10",
            discount_type=Coupon.DiscountType.FIXED,
            value="10.00",
            total_usage_limit=1,
        )
        payload = {
            "payment_mode": "cod",
            "customer": {"name": "Amina", "email": "amina@example.test"},
            "items": [item(self.product, self.variant, 2)],
            "coupon_code": "REPLAY10",
        }

        first = self._post(
            reverse("order-create"), payload, HTTP_IDEMPOTENCY_KEY="checkout-attempt-1",
        )
        second = self._post(
            reverse("order-create"), payload, HTTP_IDEMPOTENCY_KEY="checkout-attempt-1",
        )

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["order_id"], first.data["order_id"])
        self.assertEqual(Order.objects.count(), 1)
        self.variant.refresh_from_db()
        coupon.refresh_from_db()
        self.assertEqual(self.variant.reserved, 2)
        self.assertEqual(coupon.usage_count, 1)
        self.assertEqual(first.data["discount_amount"], "10.00")
        self.assertEqual(first.data["amount"], "190.00")

    def test_checkout_idempotency_rejects_a_changed_payload(self):
        path = reverse("order-create")
        payload = {
            "payment_mode": "cod",
            "customer": {"name": "Amina"},
            "items": [item(self.product, self.variant, 1)],
        }
        first = self._post(path, payload, HTTP_IDEMPOTENCY_KEY="checkout-attempt-2")
        payload["items"][0]["quantity"] = 2
        second = self._post(path, payload, HTTP_IDEMPOTENCY_KEY="checkout-attempt-2")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Order.objects.count(), 1)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 1)

    def test_checkout_returns_field_guidance_for_an_invalid_coupon(self):
        response = self._post(
            reverse("order-create"),
            {
                "payment_mode": "cod",
                "customer": {"name": "Amina"},
                "items": [item(self.product, self.variant, 1)],
                "coupon_code": "NOT-VALID",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("coupon_code", response.data)
        self.assertEqual(Order.objects.count(), 0)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)

    def test_unknown_store_404(self):
        res = self.client.post(reverse("order-create"), {}, format="json", HTTP_X_STORE_SLUG="nope")
        self.assertEqual(res.status_code, 404)

    def test_order_detail_public(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        res = self.client.get(
            reverse("order-detail", args=[order.order_id]),
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["order_id"], str(order.order_id))
        self.assertNotIn("customer_email", res.data)
        self.assertNotIn("merchant_notes", res.data)
        self.assertNotIn("reserved_quantity", res.data["items"][0])

    def test_order_detail_unknown_id(self):
        res = self.client.get(
            reverse("order-detail", args=["00000000-0000-0000-0000-000000000000"]),
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["found"])

    def test_customer_cannot_self_cancel_paid_pending_fulfillment(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["is_paid"])
        response = self.client.post(
            reverse("order-cancel", args=[order.order_id]),
            {},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(response.status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_merchant_confirm_online_order(self):
        owner = get_user_model().objects.create_user(email="boss@example.com", password="pw")
        member = StoreMember.objects.create(user=owner, store=self.store, role=StoreMember.Roles.OWNER)
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["is_paid"])
        mclient = APIClient()
        mclient.force_authenticate(owner)
        res = mclient.post(
            reverse("order-action", args=[self.store.id, "confirm"]),
            {"order_id": str(order.order_id), "acknowledged_item_ids": [order.items.get().id]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 3)
        self.assertEqual(self.variant.reserved, 0)
        order.refresh_from_db()
        self.assertEqual(order.items_acknowledged_by, member)
        self.assertIsNotNone(order.items_acknowledged_at)

        detail = mclient.get(reverse("merchant-order-detail", args=[self.store.id, order.order_id]))
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["order_id"], str(order.order_id))
        self.assertEqual(len(detail.data["items"]), 1)

    def test_shipping_requires_carrier_and_tracking_number(self):
        owner = get_user_model().objects.create_user(email="shipper@example.com", password="pw")
        StoreMember.objects.create(user=owner, store=self.store, role=StoreMember.Roles.OWNER)
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.COD,
        )
        confirm_order(order)
        mclient = APIClient()
        mclient.force_authenticate(owner)
        response = mclient.post(
            reverse("order-action", args=[self.store.id, "ship"]),
            {"order_id": str(order.order_id), "carrier": "", "tracking_number": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("carrier", response.data)
        self.assertIn("tracking_number", response.data)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CONFIRMED)

    def test_merchant_cannot_confirm_without_acknowledging_every_item(self):
        owner = get_user_model().objects.create_user(email="review@example.com", password="pw")
        StoreMember.objects.create(user=owner, store=self.store, role=StoreMember.Roles.OWNER)
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
        )
        mclient = APIClient()
        mclient.force_authenticate(owner)
        response = mclient.post(
            reverse("order-action", args=[self.store.id, "confirm"]),
            {"order_id": str(order.order_id), "acknowledged_item_ids": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)

    def test_merchant_cannot_confirm_an_unpaid_online_order(self):
        owner = get_user_model().objects.create_user(email="unpaid@example.com", password="pw")
        StoreMember.objects.create(user=owner, store=self.store, role=StoreMember.Roles.OWNER)
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        mclient = APIClient()
        mclient.force_authenticate(owner)
        response = mclient.post(
            reverse("order-action", args=[self.store.id, "confirm"]),
            {"order_id": str(order.order_id), "acknowledged_item_ids": [order.items.get().id]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.is_paid, Order.IsPaid.PENDING)

    def test_merchant_cannot_touch_other_store(self):
        outsider = get_user_model().objects.create_user(email="other@example.com", password="pw")
        mclient = APIClient()
        mclient.force_authenticate(outsider)
        res = mclient.get(reverse("merchant-orders", args=[self.store.id]))
        self.assertEqual(res.status_code, 404)

    def test_guest_order_not_linked_to_profile(self):
        order = create_order(
            self.store,
            customer={"name": "Amina"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        self.assertIsNone(order.client)

    def test_customer_order_list_is_authenticated_and_store_scoped(self):
        user = get_user_model().objects.create_user(
            email="buyer@example.com",
            password="secret12345",
            email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=user)
        StoreCustomer.objects.create(profile=profile, store=self.store)
        own = create_order(
            self.store,
            customer={"name": "Buyer"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        own.client = profile
        own.save(update_fields=["client"])
        create_order(
            self.store,
            customer={"name": "Guest"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )

        guest = self.client.get(reverse("customer-orders"), HTTP_X_STORE_SLUG=self.store.slug)
        self.assertEqual(guest.status_code, 401)

        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient()
        api.cookies[access_cookie_name("client", self.store.slug)] = access
        response = api.get(reverse("customer-orders"), HTTP_X_STORE_SLUG=self.store.slug)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["order_id"], str(own.order_id))

    def test_authenticated_checkout_links_order_to_customer_profile(self):
        user = get_user_model().objects.create_user(
            email="signed-in@example.com",
            password="secret12345",
            email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=user)
        StoreCustomer.objects.create(profile=profile, store=self.store)
        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient(enforce_csrf_checks=True)
        api.cookies[access_cookie_name("client", self.store.slug)] = access
        csrf = api.get("/api/auth/csrf/").data["csrfToken"]

        response = api.post(
            reverse("order-create"),
            {
                "payment_mode": "online",
                "customer": {"name": "Signed In"},
                "items": [item(self.product, self.variant, 1)],
            },
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Order.objects.get(order_id=response.data["order_id"]).client, profile)

    def test_authenticated_cod_checkout_accepts_generated_storefront_origin(self):
        user = get_user_model().objects.create_user(
            email="cod-customer@example.com",
            password="secret12345",
            email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=user)
        StoreCustomer.objects.create(profile=profile, store=self.store)
        access, _refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=self.store.slug)
        api = APIClient(enforce_csrf_checks=True)
        api.cookies[access_cookie_name("client", self.store.slug)] = access
        csrf = api.get("/api/auth/csrf/").data["csrfToken"]

        response = api.post(
            reverse("order-create"),
            {
                "payment_mode": "cod",
                "customer": {
                    "name": "COD Customer",
                    "email": user.email,
                    "phone": "+212612345678",
                    "city": "Casablanca",
                    "address": "18 Rue des Fleurs",
                },
                "items": [item(self.product, self.variant, 1)],
            },
            format="json",
            HTTP_ORIGIN=f"https://look.{settings.STOREFRONT_BASE_DOMAIN}",
            HTTP_X_CSRFTOKEN=csrf,
            HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(response.status_code, 201, response.data)
        order = Order.objects.get(order_id=response.data["order_id"])
        self.assertEqual(order.client, profile)
        self.assertEqual(order.payment_mode, Order.PaymentMode.COD)
        self.assertEqual(order.is_paid, Order.IsPaid.COD)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)
        self.assertEqual(self.variant.reserved, 1)

    def test_linked_order_detail_is_not_visible_anonymously(self):
        user = get_user_model().objects.create_user(
            email="private@example.com",
            password="secret12345",
            email_verified_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(user=user)
        StoreCustomer.objects.create(profile=profile, store=self.store)
        order = create_order(
            self.store,
            customer={"name": "Private"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
        )
        order.client = profile
        order.save(update_fields=["client"])

        response = self.client.get(
            reverse("order-detail", args=[order.order_id]),
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["found"])


class OrderLifecycleTests(TestCase):
    """Full lifecycle: pending → confirmed → shipped → delivered / cancelled."""

    def setUp(self):
        self.store, self.section = make_store()
        StoreConfig.objects.filter(store=self.store).update(shipping_fee=5)
        self.product, self.variant = make_product(self.store, self.section)
        self.owner = get_user_model().objects.create_user(email="lifecycle@example.com", password="pw")
        self.member = StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)

    def _create(self, mode=Order.PaymentMode.ONLINE):
        return create_order(
            self.store,
            customer={"name": "Test", "email": "test@mail.com", "phone": "0600"},
            items=[item(self.product, self.variant, 2)],
            payment_mode=mode,
        )

    def _confirm(self, order):
        if order.payment_mode == Order.PaymentMode.ONLINE:
            order.is_paid = Order.IsPaid.CONFIRMED
            order.save(update_fields=["is_paid"])
        return confirm_order(
            order,
            changed_by=self.member,
            acknowledged_item_ids=list(order.items.values_list("id", flat=True)),
        )

    def test_full_happy_path(self):
        order = self._create()
        self.assertEqual(order.status, Order.Status.PENDING)

        self._confirm(order)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CONFIRMED)
        self.assertIsNotNone(order.confirmed_at)

        ship_order(order, changed_by=self.member, tracking_number="TRK123", carrier="DHL")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.SHIPPED)
        self.assertEqual(order.tracking_number, "TRK123")
        self.assertEqual(order.carrier, "DHL")
        self.assertIsNotNone(order.shipped_at)

        deliver_order(order, changed_by=self.member)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.DELIVERED)
        self.assertIsNotNone(order.delivered_at)

    def test_cancel_pending(self):
        order = self._create()
        cancel_order(order, changed_by=self.member, reason="Changed my mind")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.assertEqual(order.cancelled_by, Order.CancelledBy.MERCHANT)
        self.assertEqual(order.cancel_reason, "Changed my mind")
        self.assertIsNotNone(order.cancelled_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)

    def test_cancel_confirmed(self):
        order = self._create(mode=Order.PaymentMode.COD)
        self._confirm(order)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 3)
        cancel_order(order, changed_by=self.member, reason="Out of stock")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.CANCELLED)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)

    def test_cannot_cancel_delivered(self):
        order = self._create()
        self._confirm(order)
        ship_order(order, changed_by=self.member, tracking_number="TRK-CANCEL", carrier="DHL")
        deliver_order(order, changed_by=self.member)
        with self.assertRaises(InvalidTransitionError):
            cancel_order(order, changed_by=self.member)

    def test_cannot_ship_pending(self):
        order = self._create()
        with self.assertRaises(InvalidTransitionError):
            ship_order(order, changed_by=self.member, tracking_number="TRK-PENDING", carrier="DHL")

    def test_cannot_deliver_pending(self):
        order = self._create()
        with self.assertRaises(InvalidTransitionError):
            deliver_order(order, changed_by=self.member)

    def test_status_history_recorded(self):
        order = self._create()
        self._confirm(order)
        ship_order(order, changed_by=self.member, tracking_number="TRK-HISTORY", carrier="DHL")
        order.refresh_from_db()
        history = list(order.status_history.values_list("from_status", "to_status"))
        self.assertEqual(history, [("pending", "confirmed"), ("confirmed", "shipped")])

    def test_merchant_notes(self):
        order = self._create()
        update_notes(order, merchant_notes="Priority customer")
        order.refresh_from_db()
        self.assertEqual(order.merchant_notes, "Priority customer")

    def test_customer_notes_on_create(self):
        order = create_order(
            self.store,
            customer={"name": "Test"},
            items=[item(self.product, self.variant, 1)],
            payment_mode=Order.PaymentMode.ONLINE,
            notes="Please gift wrap",
        )
        self.assertEqual(order.customer_notes, "Please gift wrap")

    def test_delivery_person_assignment(self):
        delivery_user = get_user_model().objects.create_user(email="driver@example.com", password="pw")
        delivery_member = StoreMember.objects.create(user=delivery_user, store=self.store, role=StoreMember.Roles.DELIVERY)
        order = self._create()
        self._confirm(order)
        ship_order(order, changed_by=self.member, tracking_number="TRK-DRIVER", carrier="DHL", delivery_person_id=delivery_member.id)
        order.refresh_from_db()
        self.assertEqual(order.delivery_person, delivery_member)

    def test_cod_order_starts_pending(self):
        order = self._create(mode=Order.PaymentMode.COD)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertIsNone(order.confirmed_at)
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.quantity, 5)
        self.assertEqual(self.variant.reserved, 2)
