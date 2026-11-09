from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import RetailType
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan
from subscriptions.services import ensure_free_subscription

from .models import Store, StoreConfig, StoreMember


class StorefrontBrandingEntitlementTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="branding@example.test", password="pass")
        self.store = Store.objects.create(
            name="Branding", slug="branding", retail_type=RetailType.objects.get(key="general"),
        )
        StoreMember.objects.create(user=self.user, store=self.store, role=StoreMember.Roles.OWNER)
        ensure_free_subscription(self.user)
        self.definition = EntitlementDefinition.objects.get(key="branding.remove_platform")
        self.plan = SubscriptionPlan.objects.get(slug="free")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def patch_preference(self, value):
        return self.client.patch(
            f"/api/store/{self.store.pk}/config/",
            {"remove_platform_branding": value}, format="json",
        )

    def public_config(self):
        client = APIClient()
        return client.get("/api/storefront/config/", HTTP_X_STORE_SLUG=self.store.slug)

    def test_direct_request_is_denied_without_entitlement(self):
        response = self.patch_preference(True)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(StoreConfig.objects.get(store=self.store).remove_platform_branding)

    def test_live_config_uses_entitlement_and_downgrade_preserves_preference(self):
        PlanEntitlement.objects.update_or_create(
            plan=self.plan, definition=self.definition, defaults={"value": True},
        )
        allowed = self.patch_preference(True)
        self.assertEqual(allowed.status_code, 200, allowed.data)
        self.assertFalse(self.public_config().data["showPlatformAttribution"])

        PlanEntitlement.objects.filter(plan=self.plan, definition=self.definition).update(value=False)
        self.assertTrue(self.public_config().data["showPlatformAttribution"])
        self.assertTrue(StoreConfig.objects.get(store=self.store).remove_platform_branding)
