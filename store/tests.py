from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from catalog.models import RetailType

from .models import Store, StoreMember


class StoreCreationReferenceDataTests(TestCase):
    def test_store_creation_restores_missing_retail_types(self):
        merchant = get_user_model().objects.create_user(
            email="reference-recovery@example.test",
            password="MerchantPass123!",
        )
        MerchantProfile.objects.create(user=merchant, business_name="Recovery merchant")
        RetailType.objects.all().delete()
        client = APIClient()
        client.force_authenticate(merchant)

        response = client.post(
            reverse("store-list"),
            {"name": "Recovered store", "slug": "recovered-store"},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        store = Store.objects.get(slug="recovered-store")
        self.assertEqual(store.retail_type.key, "general")
        self.assertTrue(store.catalog_sections.exists())


class SeedDemoCommandTests(TestCase):
    def test_preserves_merchant_owners_and_never_assigns_platform_staff(self):
        merchant = get_user_model().objects.create_user(
            email="merchant@example.test",
            password="MerchantPass123!",
        )
        MerchantProfile.objects.create(user=merchant, business_name="Local merchant")
        platform_owner = get_user_model().objects.create_superuser(
            email="platform-owner@example.test",
            password="OwnerPass123!",
        )
        owned_store = Store.objects.create(name="Owned", slug="owned")
        unowned_store = Store.objects.create(name="Unowned", slug="unowned")
        StoreMember.objects.create(
            user=merchant,
            store=owned_store,
            role=StoreMember.Roles.OWNER,
        )

        call_command(
            "seed_demo",
            orders=0,
            merchant=merchant.email,
            stdout=StringIO(),
            stderr=StringIO(),
        )

        self.assertTrue(
            StoreMember.objects.filter(
                user=merchant,
                store=owned_store,
                role=StoreMember.Roles.OWNER,
            ).exists()
        )
        self.assertTrue(
            StoreMember.objects.filter(
                user=merchant,
                store=unowned_store,
                role=StoreMember.Roles.OWNER,
            ).exists()
        )
        self.assertFalse(StoreMember.objects.filter(user=platform_owner).exists())
