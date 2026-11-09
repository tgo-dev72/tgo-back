from importlib import import_module

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan, UserSubscription


class CatalogImportTransitionTests(TestCase):
    def test_legacy_plan_and_active_snapshot_keep_import_access(self):
        legacy = EntitlementDefinition.objects.get(key="catalog.import_export")
        catalog_import = EntitlementDefinition.objects.get(key="catalog.import")
        PlanEntitlement.objects.filter(definition=catalog_import).delete()
        catalog_import.delete()
        plan = SubscriptionPlan.objects.create(
            name="Legacy import", slug="legacy-import-transition", price="10",
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        PlanEntitlement.objects.create(plan=plan, definition=legacy, value=False)
        user = get_user_model().objects.create_user(email="legacy-import@example.test", password="pass")
        subscription = UserSubscription.objects.create(
            user=user,
            plan=plan,
            status=UserSubscription.Status.ACTIVE,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            entitlements_snapshot={"catalog.import_export": False},
        )

        migration = import_module(
            "subscriptions.migrations.0011_catalog_import_and_plan_upgrade_path",
        )
        migration.forward(apps, None)

        subscription.refresh_from_db()
        self.assertTrue(subscription.entitlements_snapshot["catalog.import"])
        self.assertTrue(PlanEntitlement.objects.get(
            plan=plan, definition__key="catalog.import",
        ).value)
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_active)
