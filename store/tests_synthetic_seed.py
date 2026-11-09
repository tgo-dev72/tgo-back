from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from accounts.models import MerchantProfile
from orders.models import Order
from store.management.commands.seed_synthetic_data import DEMO_NAME, DEMO_SLUG
from store.models import Store, StoreMember


class SyntheticDataCommandTests(TestCase):
    def setUp(self):
        self.merchant = get_user_model().objects.create_user(
            email="synthetic-owner@example.test",
            password="MerchantPass123!",
        )
        MerchantProfile.objects.create(user=self.merchant)

    def seed(self, orders=40, days=30):
        call_command(
            "seed_synthetic_data",
            merchant=self.merchant.email,
            orders=orders,
            days=days,
            stdout=StringIO(),
        )
        return Store.objects.get(slug=DEMO_SLUG)

    def test_creates_isolated_dashboard_dataset(self):
        real_store = Store.objects.create(name="Merchant store", slug="merchant-store")

        demo = self.seed()

        self.assertEqual(demo.name, DEMO_NAME)
        self.assertTrue(
            StoreMember.objects.filter(
                store=demo,
                user=self.merchant,
                role=StoreMember.Roles.OWNER,
            ).exists()
        )
        self.assertEqual(demo.products.count(), 12)
        self.assertGreater(demo.stock_variants.count(), 40)
        self.assertEqual(demo.store_customers.count(), 12)
        self.assertEqual(demo.orders.count(), 40)
        self.assertEqual(demo.coupons.count(), 2)
        self.assertEqual(demo.subscribers.count(), 12)
        self.assertTrue(Store.objects.filter(pk=real_store.pk).exists())

    def test_reseed_replaces_only_the_owned_demo_store(self):
        first = self.seed(orders=20)
        first_id = first.pk
        real_order_count = Order.objects.exclude(store=first).count()

        second = self.seed(orders=25)

        self.assertNotEqual(second.pk, first_id)
        self.assertEqual(second.orders.count(), 25)
        self.assertEqual(Order.objects.exclude(store=second).count(), real_order_count)

    def test_remove_deletes_only_the_synthetic_store(self):
        demo = self.seed(orders=10)
        real_store = Store.objects.create(name="Keep me", slug="keep-me")

        call_command("seed_synthetic_data", remove=True, stdout=StringIO())

        self.assertFalse(Store.objects.filter(pk=demo.pk).exists())
        self.assertTrue(Store.objects.filter(pk=real_store.pk).exists())
