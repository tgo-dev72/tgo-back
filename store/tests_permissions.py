from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from orders.models import Order

from .models import ClientProfile, Store, StoreCustomer, StoreMember


User = get_user_model()


class StoreRoleMatrixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.store = Store.objects.create(name="Role Store", slug="role-store")
        cls.owner = User.objects.create_user(email="owner@roles.test", password="password123")
        MerchantProfile.objects.create(user=cls.owner)
        cls.admin = User.objects.create_user(email="admin@roles.test", password="password123")
        cls.staff = User.objects.create_user(email="staff@roles.test", password="password123")
        cls.delivery = User.objects.create_user(email="delivery@roles.test", password="password123")
        cls.owner_member = StoreMember.objects.create(user=cls.owner, store=cls.store, role=StoreMember.Roles.OWNER)
        cls.admin_member = StoreMember.objects.create(user=cls.admin, store=cls.store, role=StoreMember.Roles.ADMIN)
        cls.staff_member = StoreMember.objects.create(user=cls.staff, store=cls.store, role=StoreMember.Roles.STAFF)
        cls.delivery_member = StoreMember.objects.create(user=cls.delivery, store=cls.store, role=StoreMember.Roles.DELIVERY)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_delivery_role_cannot_open_configuration_catalog_customers_or_marketing(self):
        client = self.client_for(self.delivery)

        responses = [
            client.get(reverse("store-config", args=[self.store.id])),
            client.get(f"/api/catalog/store/{self.store.id}/products/"),
            client.get(reverse("store-customers", args=[self.store.id])),
            client.get(reverse("campaign-list", args=[self.store.id])),
        ]

        self.assertTrue(all(response.status_code == 404 for response in responses))

    def test_staff_can_manage_catalog_but_not_sensitive_store_configuration(self):
        client = self.client_for(self.staff)

        self.assertEqual(client.get(f"/api/catalog/store/{self.store.id}/products/").status_code, 200)
        self.assertEqual(client.get(reverse("store-config", args=[self.store.id])).status_code, 404)

    def test_customer_list_serializes_phone_number_as_json_text(self):
        customer_user = User.objects.create_user(
            email="customer@roles.test",
            password="password123",
            first_name="Customer",
            last_name="One",
        )
        profile = ClientProfile.objects.create(
            user=customer_user,
            phone="+212612345678",
        )
        StoreCustomer.objects.create(profile=profile, store=self.store)

        response = self.client_for(self.owner).get(
            reverse("store-customers", args=[self.store.id]),
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["phone"], "+212612345678")

    def test_future_payment_providers_are_blocked_and_do_not_expose_credentials(self):
        self.store.config.payment_keys = {"private_key": "existing-secret"}
        self.store.config.save(update_fields=["payment_keys"])
        response = self.client_for(self.admin).patch(
            reverse("store-config", args=[self.store.id]),
            {"payment_keys": {"private_key": "secret"}},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

        admin_read = self.client_for(self.admin).get(reverse("store-config", args=[self.store.id]))
        owner_read = self.client_for(self.owner).get(reverse("store-config", args=[self.store.id]))
        self.assertEqual(admin_read.data["payment_keys"], {})
        self.assertEqual(owner_read.data["payment_keys"], {})
        providers = {item["code"]: item for item in owner_read.data["payment_gateway"]["available_providers"]}
        self.assertEqual(set(providers), {"paypal", "youcanpay", "stripe"})
        self.assertTrue(all(item["availability"] == "future" for item in providers.values()))
        self.assertTrue(all(item["selectable"] is False for item in providers.values()))

        owner_update = self.client_for(self.owner).patch(
            reverse("store-config", args=[self.store.id]),
            {"payment_keys": {"private_key": "replacement", "public_key": "public"}},
            format="json",
        )
        self.assertEqual(owner_update.status_code, 400)
        self.store.config.refresh_from_db()
        self.assertEqual(self.store.config.payment_keys, {"private_key": "existing-secret"})

    def test_admin_cannot_create_another_owner(self):
        response = self.client_for(self.admin).post(
            reverse("store-members", args=[self.store.id]),
            {"email": "second-owner@roles.test", "role": "owner"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(email="second-owner@roles.test").exists())

    def test_owner_can_add_delivery_member_with_seats_and_gets_limit_guidance(self):
        from subscriptions.bootstrap import restore_commercial_reference_data
        from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan

        restore_commercial_reference_data()
        entitlement, _ = PlanEntitlement.objects.update_or_create(
            plan=SubscriptionPlan.objects.get(slug="free"),
            definition=EntitlementDefinition.objects.get(key="team.max_members"),
            defaults={"value": 5},
        )
        client = self.client_for(self.owner)
        payload = {"email": "new-courier@roles.test", "role": "delivery",
                   "first_name": "Sam", "last_name": "Courier", "password": "StrongPass-2026!",
                   "confirm_password": "StrongPass-2026!"}
        response = client.post(reverse("store-members", args=[self.store.id]), payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        member = StoreMember.objects.get(store=self.store, user__email=payload["email"])
        self.assertEqual(member.role, StoreMember.Roles.DELIVERY)
        self.assertTrue(member.user.check_password(payload["password"]))

        response = client.post(reverse("store-members", args=[self.store.id]),
                               {**payload, "email": "over-limit@roles.test"}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(User.objects.filter(email="over-limit@roles.test").exists())

    def test_database_allows_only_one_owner_per_store(self):
        candidate = User.objects.create_user(email="candidate@roles.test", password="password123")

        with self.assertRaises(IntegrityError), transaction.atomic():
            StoreMember.objects.create(user=candidate, store=self.store, role=StoreMember.Roles.OWNER)

    def test_invited_team_member_cannot_create_unrelated_store(self):
        response = self.client_for(self.admin).post(
            reverse("store-list"),
            {"name": "Unauthorized", "slug": "unauthorized"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Store.objects.filter(slug="unauthorized").exists())

    def test_delivery_sees_only_assigned_orders_and_can_only_deliver_them(self):
        assigned = Order.objects.create(
            store=self.store,
            status=Order.Status.SHIPPED,
            delivery_person=self.delivery_member,
        )
        other = Order.objects.create(store=self.store, status=Order.Status.SHIPPED)
        client = self.client_for(self.delivery)

        listing = client.get(reverse("merchant-orders", args=[self.store.id]))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["order_id"] for item in listing.data], [str(assigned.order_id)])

        forbidden = client.post(
            reverse("order-action", args=[self.store.id, "cancel"]),
            {"order_id": str(assigned.order_id)},
            format="json",
        )
        self.assertEqual(forbidden.status_code, 403)

        unassigned = client.post(
            reverse("order-action", args=[self.store.id, "deliver"]),
            {"order_id": str(other.order_id)},
            format="json",
        )
        self.assertEqual(unassigned.status_code, 403)

        delivered = client.post(
            reverse("order-action", args=[self.store.id, "deliver"]),
            {"order_id": str(assigned.order_id)},
            format="json",
        )
        self.assertEqual(delivered.status_code, 200)
        assigned.refresh_from_db()
        self.assertEqual(assigned.status, Order.Status.DELIVERED)
