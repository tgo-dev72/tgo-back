from django.core.exceptions import ValidationError
from django.test import TestCase

from .bootstrap import restore_commercial_reference_data
from .entitlements import configure_plan_entitlements
from .models import SubscriptionPlan
from .progression import compare_plan_progression, validate_plan_progression


class PlanProgressionTests(TestCase):
    def setUp(self):
        restore_commercial_reference_data()
        self.lower = SubscriptionPlan.objects.create(
            name="Lower", slug="progress-lower", price="20", is_active=False,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        )
        self.higher = SubscriptionPlan.objects.create(
            name="Higher", slug="progress-higher", price="40", is_active=False,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            upgrade_from=self.lower,
        )

    def configure(self, lower, higher):
        configure_plan_entitlements(self.lower, lower)
        configure_plan_entitlements(self.higher, higher)

    def test_boolean_capability_cannot_disappear(self):
        self.configure({"catalog.import": True}, {"catalog.import": False})
        conflicts = compare_plan_progression(self.lower, self.higher)
        self.assertEqual([item.key for item in conflicts], ["catalog.import"])

    def test_finite_allowances_cannot_decrease(self):
        self.configure({"stores.max_count": 3}, {"stores.max_count": 2})
        self.assertIn("stores.max_count", [item.key for item in compare_plan_progression(self.lower, self.higher)])

    def test_product_zero_is_semantic_unlimited(self):
        self.configure({"catalog.max_products": 0}, {"catalog.max_products": 500})
        self.assertIn("catalog.max_products", [item.key for item in compare_plan_progression(self.lower, self.higher)])
        self.configure({"catalog.max_products": 500}, {"catalog.max_products": 0})
        self.assertNotIn("catalog.max_products", [item.key for item in compare_plan_progression(self.lower, self.higher)])

    def test_analytics_uses_semantic_order(self):
        self.configure({"analytics.level": "advanced"}, {"analytics.level": "standard"})
        self.assertIn("analytics.level", [item.key for item in compare_plan_progression(self.lower, self.higher)])

    def test_incompatible_subscription_scopes_are_rejected(self):
        self.higher.entitlement_scope = SubscriptionPlan.EntitlementScope.ONE_STORE
        self.higher.save(update_fields=["entitlement_scope"])
        conflicts = compare_plan_progression(self.lower, self.higher)
        self.assertEqual([item.key for item in conflicts], ["entitlement_scope"])

    def test_explicit_active_upgrade_path_is_validated(self):
        self.configure({"team.max_members": 4}, {"team.max_members": 2})
        self.higher.is_active = True
        self.higher.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError) as raised:
            validate_plan_progression(self.higher)
        self.assertIn("team.max_members", str(raised.exception))
