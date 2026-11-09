from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from coupons.models import Coupon, CouponRedemption
from orders.models import Order, QuantityException
from orders.tests import item, make_product, make_store
from store.models import StoreConfig


class CheckoutQuoteTests(TestCase):
    def setUp(self):
        self.store, section = make_store(slug="quote-store")
        StoreConfig.objects.filter(store=self.store).update(shipping_fee=30)
        self.product, self.variant = make_product(self.store, section, quantity=5)
        self.coupon = Coupon.objects.create(
            store=self.store, code="REVIEW10", discount_type="percentage", value=10,
            total_usage_limit=1, per_customer_usage_limit=1,
        )
        self.client = APIClient()
        self.payload = {
            "payment_mode": "cod", "customer": {"email": "buyer@example.test"},
            "items": [item(self.product, self.variant, 1)], "coupon_code": "REVIEW10",
        }

    def post(self, route, payload=None):
        return self.client.post(reverse(route), payload or self.payload, format="json",
                                HTTP_X_STORE_SLUG=self.store.slug)

    def reviewed_payload(self):
        response = self.post("order-quote")
        self.assertEqual(response.status_code, 200, response.data)
        return {**self.payload, "expected_totals": {
            key: response.data[key] for key in ("subtotal", "shipping", "discount", "total", "currency")
        }}

    def test_repeated_review_does_not_create_order_reserve_stock_or_consume_coupon(self):
        for _ in range(2):
            response = self.post("order-quote")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["subtotal"], Decimal("100.00"))
            self.assertEqual(response.data["shipping"], Decimal("30.00"))
            self.assertEqual(response.data["discount"], Decimal("10.00"))
            self.assertEqual(response.data["total"], Decimal("120.00"))
        self.variant.refresh_from_db()
        self.coupon.refresh_from_db()
        self.assertEqual(self.variant.reserved, 0)
        self.assertEqual(self.coupon.usage_count, 0)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(CouponRedemption.objects.exists())

    def test_confirm_reviewed_order_has_same_total_and_consumes_one_use(self):
        response = self.post("order-create", self.reviewed_payload())
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["amount"], "120.00")
        self.assertEqual(response.data["coupon_code"], "REVIEW10")
        self.coupon.refresh_from_db()
        self.assertEqual(self.coupon.usage_count, 1)

    def test_shipping_or_currency_change_rejects_confirmation_without_side_effects(self):
        for field, value in (("shipping_fee", 40), ("currency", "EUR")):
            with self.subTest(field=field):
                StoreConfig.objects.filter(store=self.store).update(shipping_fee=30, currency="MAD")
                payload = self.reviewed_payload()
                StoreConfig.objects.filter(store=self.store).update(**{field: value})
                response = self.post("order-create", payload)
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["code"], "checkout_changed")
                self.variant.refresh_from_db()
                self.coupon.refresh_from_db()
                self.assertEqual(self.variant.reserved, 0)
                self.assertEqual(self.coupon.usage_count, 0)
                self.assertFalse(Order.objects.exists())
                self.assertFalse(CouponRedemption.objects.exists())

    def test_changed_selling_price_rejects_confirmation_and_preserves_coupon(self):
        payload = self.reviewed_payload()
        self.variant.price = 200
        self.variant.save(update_fields=["price"])
        response = self.post("order-create", payload)
        self.assertEqual(response.status_code, 409, response.data)
        self.coupon.refresh_from_db()
        self.variant.refresh_from_db()
        self.assertEqual(self.coupon.usage_count, 0)
        self.assertEqual(self.variant.reserved, 0)
        self.assertFalse(Order.objects.exists())

    def test_stock_change_cannot_silently_reduce_reviewed_order(self):
        payload = self.reviewed_payload()
        self.variant.quantity = 0
        self.variant.save(update_fields=["quantity"])
        response = self.post("order-create", payload)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(QuantityException.objects.exists())

    def test_quote_rejects_invalid_coupon_and_duplicate_lines_exceeding_stock(self):
        response = self.post("order-quote", {**self.payload, "coupon_code": "UNKNOWN"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("coupon_code", response.data)
        response = self.post("order-quote", {**self.payload, "items": [item(self.product, self.variant, 3)] * 2})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_quote_does_not_read_other_tenants_products(self):
        other, section = make_store(slug="other-quote-store")
        product, variant = make_product(other, section)
        response = self.post("order-quote", {**self.payload, "items": [item(product, variant, 1)]})
        self.assertEqual(response.status_code, 400)
