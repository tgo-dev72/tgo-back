from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import CatalogSection, Product
from store.models import Store, StoreMember

from .models import Coupon


class CouponApiTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(email="owner@example.test", password="Pass12345!")
        self.other = get_user_model().objects.create_user(email="other@example.test", password="Pass12345!")
        self.staff = get_user_model().objects.create_user(email="staff@example.test", password="Pass12345!")
        self.store = Store.objects.create(name="Main", slug="main")
        self.other_store = Store.objects.create(name="Other", slug="other")
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(user=self.other, store=self.other_store, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(user=self.staff, store=self.store, role=StoreMember.Roles.STAFF)
        self.category = CatalogSection.objects.create(store=self.store, key="clothes", label="Clothes")
        self.other_category = CatalogSection.objects.create(store=self.other_store, key="other", label="Other")
        self.product = Product.objects.create(
            store=self.store,
            section=self.category,
            name="Shirt",
            ref="SHIRT-1",
            price="100.00",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = f"/api/store/{self.store.id}/coupons/"

    def test_owner_can_create_and_list_normalized_coupon(self):
        response = self.client.post(
            self.url,
            {
                "code": " welcome-10 ",
                "discount_type": "percentage",
                "value": "10.00",
                "scope": "products",
                "product_ids": [self.product.id],
                "minimum_subtotal": "50.00",
                "maximum_discount": "25.00",
                "total_usage_limit": 100,
                "per_customer_usage_limit": 1,
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["code"], "WELCOME-10")
        self.assertEqual(response.data["availability"], "active")
        self.assertEqual(response.data["product_ids"], [self.product.id])
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_scope_targets_must_belong_to_the_same_store(self):
        response = self.client.post(
            self.url,
            {
                "code": "WRONG",
                "discount_type": "fixed",
                "value": "10.00",
                "scope": "categories",
                "category_ids": [self.other_category.id],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("category_ids", response.data)

    def test_coupon_rules_reject_invalid_percentage_and_schedule(self):
        response = self.client.post(
            self.url,
            {
                "code": "INVALID",
                "discount_type": "percentage",
                "value": "101.00",
                "scope": "all_products",
                "starts_at": "2030-01-02T00:00:00Z",
                "ends_at": "2030-01-01T00:00:00Z",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("value", response.data)

    def test_coupon_code_is_unique_per_store(self):
        Coupon.objects.create(
            store=self.store,
            code="SAVE10",
            discount_type=Coupon.DiscountType.FIXED,
            value="10.00",
        )
        response = self.client.post(
            self.url,
            {
                "code": "save10",
                "discount_type": "fixed",
                "value": "5.00",
                "scope": "all_products",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_non_manager_cannot_manage_store_coupons(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_patch_scope_clears_irrelevant_targets(self):
        coupon = Coupon.objects.create(
            store=self.store,
            code="CATEGORY",
            discount_type=Coupon.DiscountType.FIXED,
            value="5.00",
            scope=Coupon.Scope.CATEGORIES,
        )
        coupon.categories.add(self.category)
        response = self.client.patch(
            f"{self.url}{coupon.id}/",
            {"scope": "all_products", "category_ids": []},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(coupon.categories.exists())

    def test_model_relations_reject_cross_store_targets(self):
        coupon = Coupon.objects.create(
            store=self.store,
            code="TENANT",
            discount_type=Coupon.DiscountType.FIXED,
            value="5.00",
        )
        foreign_product = Product.objects.create(
            store=self.other_store,
            section=self.other_category,
            name="Foreign",
            ref="FOREIGN",
            price="10.00",
        )
        with self.assertRaises(ValidationError), transaction.atomic():
            coupon.categories.add(self.other_category)
        with self.assertRaises(ValidationError), transaction.atomic():
            coupon.products.add(foreign_product)
