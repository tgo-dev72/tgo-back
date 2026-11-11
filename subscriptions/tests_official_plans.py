from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from platform_admin.models import PlatformCommerceSettings

from .bootstrap import restore_commercial_reference_data
from .models import PlanEntitlement, SubscriptionPlan
from .official_plans import OFFICIAL_ENTITLEMENT_KEYS, OFFICIAL_PLANS
from .services import ensure_free_subscription


class OfficialPlanCatalogTests(TestCase):
    def test_official_catalog_is_exact_and_idempotent(self):
        restore_commercial_reference_data()
        restore_commercial_reference_data()
        plans = list(SubscriptionPlan.objects.filter(slug__in=OFFICIAL_PLANS).order_by("sort_order"))
        self.assertEqual([plan.slug for plan in plans], ["free", "starter", "growth", "business"])
        self.assertEqual([plan.price for plan in plans], [config["price"] for config in OFFICIAL_PLANS.values()])
        self.assertEqual([plan.currency for plan in plans], ["MAD"] * 4)
        self.assertEqual([plan.is_featured for plan in plans], [False, False, True, False])
        for plan in plans:
            values = dict(PlanEntitlement.objects.filter(
                plan=plan, definition__key__in=OFFICIAL_ENTITLEMENT_KEYS,
            ).values_list("definition__key", "value"))
            self.assertEqual(values, OFFICIAL_PLANS[plan.slug]["entitlements"])
            self.assertEqual(len(values), 10)

    def test_legacy_premium_is_preserved_but_not_sold(self):
        legacy = SubscriptionPlan.objects.get(slug="premium")
        restore_commercial_reference_data()
        legacy.refresh_from_db()
        self.assertFalse(legacy.is_active)

    def test_free_period_defaults_to_one_month_and_is_granted_once(self):
        settings = PlatformCommerceSettings.load()
        self.assertEqual(settings.free_trial_days, 30)
        user = get_user_model().objects.create_user(email="month-trial@example.com", password="test-pass-123")
        before = timezone.now()
        subscription = ensure_free_subscription(user)
        self.assertGreaterEqual(subscription.current_period_end, before + timedelta(days=30) - timedelta(seconds=2))
        original_end = subscription.current_period_end
        subscription.status = subscription.Status.EXPIRED
        subscription.current_period_end = before - timedelta(seconds=1)
        subscription.save(update_fields=["status", "current_period_end", "updated_at"])
        repeated = ensure_free_subscription(user)
        self.assertEqual(repeated.pk, subscription.pk)
        self.assertEqual(repeated.status, subscription.Status.EXPIRED)
        self.assertNotEqual(repeated.current_period_end, original_end)
