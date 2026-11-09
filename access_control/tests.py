from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import CatalogSection, Product
from store.models import Store, StoreMember
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan

from .models import AccessAuditEvent, AccessGroup, AccessRole, MemberAccess, StaffInvitation
from .services import effective_permissions, hash_token, issue_token

User = get_user_model()


class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner_a = User.objects.create_user(email="owner-a@example.test", password="StrongPassword123!")
        cls.owner_b = User.objects.create_user(email="owner-b@example.test", password="StrongPassword123!")
        cls.staff = User.objects.create_user(email="staff@example.test", password="StrongPassword123!")
        cls.store_a = Store.objects.create(name="Store A", slug="store-a")
        cls.store_b = Store.objects.create(name="Store B", slug="store-b")
        cls.owner_member_a = StoreMember.objects.create(user=cls.owner_a, store=cls.store_a, role="owner")
        cls.owner_member_b = StoreMember.objects.create(user=cls.owner_b, store=cls.store_b, role="owner")
        cls.staff_member = StoreMember.objects.create(user=cls.staff, store=cls.store_a, role="staff")
        PlanEntitlement.objects.update_or_create(
            plan=SubscriptionPlan.objects.get(slug="free"),
            definition=EntitlementDefinition.objects.get(key="team.max_members"),
            defaults={"value": 3},
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_owner_can_create_custom_role_and_other_tenant_cannot_see_it(self):
        response = self.client_for(self.owner_a).post(
            reverse("access-roles", args=[self.store_a.pk]),
            {"key": "warehouse", "name": "Warehouse", "permissions": ["inventory.view", "inventory.update"], "field_permissions": {}},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        role = AccessRole.objects.get(pk=response.data["id"])
        self.assertEqual(role.store, self.store_a)
        self.assertEqual(self.client_for(self.owner_b).get(reverse("access-role-detail", args=[self.store_a.pk, role.pk])).status_code, 404)

    def test_group_roles_are_inherited_and_suspension_takes_effect_immediately(self):
        role = AccessRole.objects.create(store=self.store_a, key="support", name="Support", permissions=["orders.view"])
        group = AccessGroup.objects.create(store=self.store_a, name="Support team")
        group.roles.add(role)
        group.members.add(self.staff_member)
        self.assertIn("orders.view", effective_permissions(self.staff_member))
        profile = MemberAccess.objects.create(membership=self.staff_member, is_active=False)
        response = self.client_for(self.staff).get(reverse("access-effective", args=[self.store_a.pk]))
        self.assertEqual(response.status_code, 403)
        profile.is_active = True
        profile.save(update_fields=["is_active"])

    def test_non_owner_cannot_delegate_permissions_they_do_not_have(self):
        limited = AccessRole.objects.create(store=self.store_a, key="limited", name="Limited", permissions=["roles.view", "roles.manage"])
        self.staff_member.access_roles.add(limited)
        response = self.client_for(self.staff).post(
            reverse("access-roles", args=[self.store_a.pk]),
            {"key": "escalated", "name": "Escalated", "permissions": ["billing.manage"], "field_permissions": {}},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AccessRole.objects.filter(store=self.store_a, key="escalated").exists())

    def test_product_sensitive_field_is_hidden_and_write_is_rejected(self):
        section = CatalogSection.objects.create(store=self.store_a, key="general", label="General")
        product = Product.objects.create(store=self.store_a, section=section, name="Item", slug="item", ref="ITEM", price="100.00", cost_price="50.00")
        role = AccessRole.objects.create(
            store=self.store_a, key="catalog-limited", name="Catalog limited",
            permissions=["products.view", "products.update"],
            field_permissions={"products": {"name": {"view": True, "edit": True}, "cost_price": {"view": False, "edit": False}}},
        )
        self.staff_member.access_roles.add(role)
        detail = self.client_for(self.staff).get(f"/api/catalog/store/{self.store_a.pk}/products/{product.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("cost_price", detail.data)
        update = self.client_for(self.staff).patch(
            f"/api/catalog/store/{self.store_a.pk}/products/{product.pk}/",
            {"cost_price": "1.00"}, format="json",
        )
        self.assertEqual(update.status_code, 403)
        product.refresh_from_db()
        self.assertEqual(str(product.cost_price), "50.00")

    def test_invitation_is_single_use_tenant_scoped_and_audited(self):
        role = AccessRole.objects.create(store=self.store_a, key="analyst", name="Analyst", permissions=["analytics.view"])
        raw, digest = issue_token()
        invitation = StaffInvitation.objects.create(store=self.store_a, email="invitee@example.test", token_hash=digest, invited_by=self.owner_a, expires_at=timezone.now() + timedelta(days=1))
        invitation.roles.add(role)
        payload = {"token": raw, "password": "StrongPassword123!", "first_name": "Invited"}
        accepted = APIClient().post(reverse("access-invitation-accept"), payload, format="json")
        self.assertEqual(accepted.status_code, 200, accepted.data)
        member = StoreMember.objects.get(store=self.store_a, user__email="invitee@example.test")
        self.assertEqual(list(member.access_roles.values_list("pk", flat=True)), [role.pk])
        self.assertFalse(StoreMember.objects.filter(store=self.store_b, user=member.user).exists())
        self.assertEqual(APIClient().post(reverse("access-invitation-accept"), payload, format="json").status_code, 400)
        self.assertTrue(AccessAuditEvent.objects.filter(store=self.store_a, action="invitation.accepted").exists())

    def test_expired_invitation_fails_closed(self):
        raw, digest = issue_token()
        invitation = StaffInvitation.objects.create(store=self.store_a, email="late@example.test", token_hash=digest, invited_by=self.owner_a, expires_at=timezone.now() - timedelta(seconds=1))
        response = APIClient().post(reverse("access-invitation-accept"), {"token": raw, "password": "StrongPassword123!"}, format="json")
        self.assertEqual(response.status_code, 400)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "expired")

    def test_invitation_acceptance_fails_when_reserved_seats_exceed_plan(self):
        PlanEntitlement.objects.filter(
            plan__slug="free", definition__key="team.max_members",
        ).update(value=2)
        raw, digest = issue_token()
        invitation = StaffInvitation.objects.create(
            store=self.store_a, email="over-limit@example.test", token_hash=digest,
            invited_by=self.owner_a, expires_at=timezone.now() + timedelta(days=1),
        )
        response = APIClient().post(
            reverse("access-invitation-accept"),
            {"token": raw, "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.data)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, "pending")
        self.assertFalse(StoreMember.objects.filter(store=self.store_a, user__email="over-limit@example.test").exists())
