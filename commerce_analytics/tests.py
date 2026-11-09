import uuid
from io import StringIO
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import MerchantProfile
from catalog.models import CatalogSection, Product, StockVariant
from orders.models import Order, OrderedProduct
from store.models import Store, StoreMember

from .models import InventorySnapshot, StorefrontEvent
from .services import build_store_report, capture_inventory_snapshot, is_collected_revenue_order


User = get_user_model()


class AnalyticsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user(email="analytics-owner@test.local", password="password123")
        MerchantProfile.objects.create(user=cls.owner)
        cls.staff = User.objects.create_user(email="analytics-staff@test.local", password="password123")
        cls.delivery = User.objects.create_user(email="analytics-delivery@test.local", password="password123")
        cls.outsider = User.objects.create_user(email="analytics-outsider@test.local", password="password123")
        cls.store = Store.objects.create(name="Analytics Store", slug="analytics-store")
        cls.other_store = Store.objects.create(name="Other Store", slug="other-analytics-store")
        StoreMember.objects.create(user=cls.owner, store=cls.store, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(user=cls.staff, store=cls.store, role=StoreMember.Roles.STAFF)
        StoreMember.objects.create(user=cls.delivery, store=cls.store, role=StoreMember.Roles.DELIVERY)
        cls.section = CatalogSection.objects.create(store=cls.store, key="all", label="All")
        cls.product = Product.objects.create(
            store=cls.store, section=cls.section, name="Product", ref="P-1", price=Decimal("40.00"),
        )
        cls.variant = cls.product.variants.first() or StockVariant.objects.create(
            store=cls.store, product=cls.product, quantity=0,
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def create_order(self, *, amount, payment_mode, paid, status, days_ago=0):
        order = Order.objects.create(
            store=self.store,
            amount=Decimal(amount),
            payment_mode=payment_mode,
            is_paid=paid,
            status=status,
            customer_email=f"customer-{uuid.uuid4()}@test.local",
            delivered_at=timezone.now() if status == Order.Status.DELIVERED else None,
        )
        created_at = timezone.now() - timedelta(days=days_ago)
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.created_at = created_at
        return order


class RevenueReportTests(AnalyticsTestCase):
    def test_collected_revenue_does_not_treat_undelivered_cod_as_cash(self):
        online = self.create_order(
            amount="100", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.CONFIRMED, status=Order.Status.CONFIRMED,
        )
        cod_pending = self.create_order(
            amount="70", payment_mode=Order.PaymentMode.COD,
            paid=Order.IsPaid.COD, status=Order.Status.CONFIRMED,
        )
        cod_delivered = self.create_order(
            amount="50", payment_mode=Order.PaymentMode.COD,
            paid=Order.IsPaid.COD, status=Order.Status.DELIVERED,
        )
        self.assertTrue(is_collected_revenue_order(online))
        self.assertFalse(is_collected_revenue_order(cod_pending))
        self.assertTrue(is_collected_revenue_order(cod_delivered))

        report = build_store_report(self.store, 30, level="advanced")

        self.assertEqual(report["kpis"]["collected_revenue"], "150.00")
        self.assertEqual(report["standard"]["payment"], {"online": "100.00", "cod": "50.00"})

    def test_product_rankings_only_use_collected_orders(self):
        paid = self.create_order(
            amount="80", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.CONFIRMED, status=Order.Status.CONFIRMED,
        )
        unpaid = self.create_order(
            amount="400", payment_mode=Order.PaymentMode.COD,
            paid=Order.IsPaid.COD, status=Order.Status.CONFIRMED,
        )
        for order, quantity in ((paid, 2), (unpaid, 10)):
            OrderedProduct.objects.create(
                order=order, store=self.store, product_id=self.product.pk,
                variant_id=self.variant.pk, section_key="all", ref="P-1",
                name="Product", quantity=quantity, fulfilled_quantity=quantity,
                price=Decimal("40.00"),
            )

        report = build_store_report(self.store, 30, level="standard")

        self.assertEqual(report["standard"]["top_products"][0]["quantity"], 2)
        self.assertEqual(report["standard"]["top_products"][0]["revenue"], "80.00")


class MerchantAnalyticsApiTests(AnalyticsTestCase):
    def test_operations_staff_can_read_but_delivery_and_outsider_cannot(self):
        url = reverse("merchant-analytics", args=[self.store.pk])

        self.assertEqual(self.client_for(self.staff).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.delivery).get(url).status_code, 404)
        self.assertEqual(self.client_for(self.outsider).get(url).status_code, 404)

    @patch("commerce_analytics.views.store_analytics_level", return_value="standard")
    def test_range_is_enforced_by_server_side_entitlement(self, _level):
        response = self.client_for(self.owner).get(
            reverse("merchant-analytics", args=[self.store.pk]), {"range": 365},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["available_ranges"], [7, 30, 90])


class StorefrontEventApiTests(AnalyticsTestCase):
    def test_authentication_funnel_event_accepts_no_personal_data(self):
        payload = {"event_id": str(uuid.uuid4()), "event_type": "login_failed", "session_id": "qa-session"}
        url = reverse("storefront-analytics-event")
        client = APIClient()
        accepted = client.post(url, payload, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        rejected = client.post(
            url,
            {**payload, "event_id": str(uuid.uuid4()), "properties": {"email": "buyer@example.test"}},
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertEqual(rejected.status_code, 400, rejected.content)
        self.assertEqual(StorefrontEvent.objects.get(event_id=payload["event_id"]).properties, {})

    def test_event_is_hashed_and_idempotent(self):
        event_id = uuid.uuid4()
        payload = {
            "event_id": str(event_id),
            "event_type": "product_view",
            "visitor_id": "raw-browser-id",
            "session_id": "raw-session-id",
            "product_id": self.product.pk,
            "properties": {"path": f"/products/{self.product.slug}"},
        }
        client = APIClient()
        url = reverse("storefront-analytics-event")

        first = client.post(url, payload, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        second = client.post(url, payload, format="json", HTTP_X_STORE_SLUG=self.store.slug)

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["deduplicated"])
        event = StorefrontEvent.objects.get(store=self.store, event_id=event_id)
        self.assertNotEqual(event.visitor_hash, "raw-browser-id")
        self.assertNotIn("raw-browser-id", event.visitor_hash)
        self.assertNotEqual(event.session_hash, "raw-session-id")
        self.assertNotIn("raw-session-id", event.session_hash)

    def test_reused_event_identifier_cannot_change_the_recorded_action(self):
        event_id = uuid.uuid4()
        client = APIClient()
        url = reverse("storefront-analytics-event")
        first = client.post(
            url,
            {"event_id": str(event_id), "event_type": "page_view", "session_id": "session"},
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )
        conflicting = client.post(
            url,
            {
                "event_id": str(event_id), "event_type": "product_view",
                "session_id": "session", "product_id": self.product.pk,
            },
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(conflicting.status_code, 409)
        event = StorefrontEvent.objects.get(store=self.store, event_id=event_id)
        self.assertEqual(event.event_type, StorefrontEvent.EventType.PAGE_VIEW)
        self.assertIsNone(event.product_id)

    def test_legacy_purchase_is_normalized_to_order_submitted(self):
        order = self.create_order(
            amount="40", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.PENDING, status=Order.Status.PENDING,
        )
        response = APIClient().post(
            reverse("storefront-analytics-event"),
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "purchase",
                "session_id": "checkout-session",
                "order_id": str(order.order_id),
            },
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(StorefrontEvent.objects.latest("id").event_type, StorefrontEvent.EventType.ORDER_SUBMITTED)

    def test_event_contract_rejects_missing_entities_and_sensitive_searches(self):
        client = APIClient()
        url = reverse("storefront-analytics-event")
        missing_product = client.post(
            url,
            {"event_id": str(uuid.uuid4()), "event_type": "add_to_cart", "session_id": "session"},
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )
        sensitive_search = client.post(
            url,
            {
                "event_id": str(uuid.uuid4()), "event_type": "search", "session_id": "session",
                "properties": {"query": "person@example.com", "results_count": 0},
            },
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(missing_product.status_code, 400)
        self.assertEqual(sensitive_search.status_code, 400)
        self.assertFalse(StorefrontEvent.objects.filter(store=self.store).exists())

    def test_event_contract_rejects_invalid_counts_and_sensitive_attribution(self):
        client = APIClient()
        url = reverse("storefront-analytics-event")
        invalid_count = client.post(
            url,
            {
                "event_id": str(uuid.uuid4()), "event_type": "search", "session_id": "session",
                "properties": {"query": "shirt", "results_count": -1},
            },
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )
        sensitive_campaign = client.post(
            url,
            {
                "event_id": str(uuid.uuid4()), "event_type": "page_view", "session_id": "session",
                "properties": {"source": "newsletter", "campaign": "person@example.com"},
            },
            format="json", HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(invalid_count.status_code, 400)
        self.assertEqual(sensitive_campaign.status_code, 400)
        self.assertFalse(StorefrontEvent.objects.filter(store=self.store).exists())

    def test_probable_bot_event_is_ignored_before_storage(self):
        response = APIClient().post(
            reverse("storefront-analytics-event"),
            {"event_id": str(uuid.uuid4()), "event_type": "page_view", "session_id": "bot-session"},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
            HTTP_USER_AGENT="Googlebot/2.1",
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertFalse(StorefrontEvent.objects.filter(store=self.store).exists())

    def test_event_rejects_cross_store_product_and_personal_fields(self):
        client = APIClient()
        url = reverse("storefront-analytics-event")
        cross_store = client.post(
            url,
            {"event_id": str(uuid.uuid4()), "event_type": "product_view", "product_id": self.product.pk},
            format="json",
            HTTP_X_STORE_SLUG=self.other_store.slug,
        )
        personal_data = client.post(
            url,
            {"event_id": str(uuid.uuid4()), "event_type": "page_view", "properties": {"email": "x@test.local"}},
            format="json",
            HTTP_X_STORE_SLUG=self.store.slug,
        )

        self.assertEqual(cross_store.status_code, 404)
        self.assertEqual(personal_data.status_code, 400)

    def test_retention_command_only_removes_expired_events(self):
        old = StorefrontEvent.objects.create(
            store=self.store,
            event_type=StorefrontEvent.EventType.PAGE_VIEW,
            occurred_at=timezone.now() - timedelta(days=400),
        )
        recent = StorefrontEvent.objects.create(
            store=self.store,
            event_type=StorefrontEvent.EventType.PAGE_VIEW,
            occurred_at=timezone.now() - timedelta(days=10),
        )

        call_command("purge_storefront_events", days=395, stdout=StringIO())

        self.assertFalse(StorefrontEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(StorefrontEvent.objects.filter(pk=recent.pk).exists())


class ConversionIntelligenceTests(AnalyticsTestCase):
    def event(self, event_type, session, *, product=None, order=None, properties=None, days_ago=0):
        return StorefrontEvent.objects.create(
            store=self.store,
            event_type=event_type,
            event_id=uuid.uuid4(),
            session_hash=session,
            product=product,
            order=order,
            properties=properties or {},
            occurred_at=timezone.now() - timedelta(days=days_ago),
        )

    def test_standard_funnel_and_products_tolerate_missing_steps_and_failed_payment(self):
        failed_order = self.create_order(
            amount="40", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.FAILED, status=Order.Status.PENDING,
        )
        OrderedProduct.objects.create(
            order=failed_order, store=self.store, product_id=self.product.pk,
            variant_id=self.variant.pk, section_key="all", ref="P-1", name="Product",
            quantity=1, fulfilled_quantity=1, price=Decimal("40.00"),
        )
        for event_type in (
            StorefrontEvent.EventType.PAGE_VIEW,
            StorefrontEvent.EventType.PRODUCT_VIEW,
            StorefrontEvent.EventType.PRODUCT_VIEW,
            StorefrontEvent.EventType.ADD_TO_CART,
            StorefrontEvent.EventType.CHECKOUT_STARTED,
        ):
            self.event(
                event_type, "session-one",
                product=self.product if event_type in {"product_view", "add_to_cart"} else None,
            )
        self.event(StorefrontEvent.EventType.ORDER_SUBMITTED, "session-one", order=failed_order)
        self.event(StorefrontEvent.EventType.PAGE_VIEW, "session-two")
        self.event(StorefrontEvent.EventType.PRODUCT_VIEW, "session-two", product=self.product)
        self.event(StorefrontEvent.EventType.ADD_TO_CART, "session-two", product=self.product)
        self.event(
            StorefrontEvent.EventType.SEARCH, "session-two",
            properties={"query": "missing hat", "results_count": 0},
        )

        report = build_store_report(self.store, 30, level="standard")
        funnel = report["standard"]["funnel"]
        product = report["standard"]["product_performance"][0]

        self.assertEqual(funnel["store_visits"], 2)
        self.assertEqual(funnel["product_views"], 3)
        self.assertEqual(funnel["unique_sessions"]["product_views"], 2)
        self.assertEqual(funnel["rates"]["add_to_cart"], 100)
        self.assertEqual(funnel["rates"]["checkout_start"], 50)
        self.assertEqual(funnel["rates"]["cart_abandonment"], 50)
        self.assertEqual(product["orders"], 1)
        self.assertEqual(product["collected_orders"], 0)
        self.assertEqual(product["revenue"], "0.00")
        self.assertEqual(report["standard"]["zero_result_searches"], [{"query": "missing hat", "searches": 1}])

    def test_empty_event_history_returns_safe_zero_rates(self):
        report = build_store_report(self.store, 30, level="standard")

        self.assertEqual(report["standard"]["funnel"]["store_visits"], 0)
        self.assertTrue(all(value == 0 for value in report["standard"]["funnel"]["rates"].values()))
        self.assertEqual(report["standard"]["product_performance"], [])

    def test_missing_prior_stage_events_cannot_create_impossible_rates(self):
        order = self.create_order(
            amount="40", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.PENDING, status=Order.Status.PENDING,
        )
        self.event(StorefrontEvent.EventType.PRODUCT_VIEW, "view-only", product=self.product)
        self.event(StorefrontEvent.EventType.ADD_TO_CART, "cart-only", product=self.product)
        self.event(StorefrontEvent.EventType.CHECKOUT_STARTED, "checkout-only")
        self.event(StorefrontEvent.EventType.ORDER_SUBMITTED, "submitted-only", order=order)

        rates = build_store_report(self.store, 30, level="standard")["standard"]["funnel"]["rates"]

        self.assertEqual(rates["add_to_cart"], 0)
        self.assertEqual(rates["checkout_start"], 0)
        self.assertEqual(rates["order_submission"], 0)
        self.assertEqual(rates["cart_abandonment"], 100)
        self.assertEqual(rates["checkout_abandonment"], 100)

    def test_advanced_report_compares_funnels_and_uses_orders_for_source_revenue(self):
        collected = self.create_order(
            amount="80", payment_mode=Order.PaymentMode.ONLINE,
            paid=Order.IsPaid.CONFIRMED, status=Order.Status.CONFIRMED,
        )
        OrderedProduct.objects.create(
            order=collected, store=self.store, product_id=self.product.pk,
            variant_id=self.variant.pk, section_key="all", ref="P-1", name="Product",
            quantity=2, fulfilled_quantity=2, price=Decimal("40.00"),
        )
        attribution = {"source": "instagram", "campaign": "launch"}
        self.event(StorefrontEvent.EventType.PAGE_VIEW, "current-session", properties=attribution)
        for _index in range(10):
            self.event(StorefrontEvent.EventType.PRODUCT_VIEW, "current-session", product=self.product, properties=attribution)
        self.event(StorefrontEvent.EventType.ORDER_SUBMITTED, "current-session", order=collected, properties=attribution)
        self.event(StorefrontEvent.EventType.PAGE_VIEW, "previous-session", properties=attribution, days_ago=35)

        report = build_store_report(self.store, 30, level="advanced")
        advanced = report["advanced"]

        self.assertEqual(advanced["funnel_comparison"]["previous"]["store_visits"], 1)
        self.assertEqual(advanced["funnel_comparison"]["change_percent"]["store_visits"], 0)
        self.assertEqual(advanced["sources"][0]["source"], "instagram")
        self.assertEqual(advanced["sources"][0]["collected_orders"], 1)
        self.assertEqual(advanced["sources"][0]["revenue"], "80.00")
        self.assertEqual(advanced["product_intelligence"][0]["revenue_per_view"], "8.00")
        self.assertEqual(len(advanced["funnel_trend"]), 30)

    def test_high_traffic_without_orders_creates_stable_opportunity_signal(self):
        self.event(StorefrontEvent.EventType.PAGE_VIEW, "visit")
        for index in range(10):
            self.event(StorefrontEvent.EventType.PRODUCT_VIEW, f"viewer-{index}", product=self.product)

        report = build_store_report(self.store, 30, level="advanced")

        self.assertEqual(report["advanced"]["opportunities"][0]["kind"], "high_traffic_low_conversion")
        self.assertEqual(report["advanced"]["opportunities"][0]["product_id"], self.product.pk)


class InventorySnapshotTests(AnalyticsTestCase):
    def test_snapshot_is_idempotent_and_updates_same_day(self):
        self.variant.quantity = 12
        self.variant.reserved = 2
        self.variant.save(update_fields=["quantity", "reserved"])

        capture_inventory_snapshot(self.store)
        self.variant.quantity = 9
        self.variant.save(update_fields=["quantity"])
        capture_inventory_snapshot(self.store)

        self.assertEqual(InventorySnapshot.objects.filter(store=self.store).count(), 1)
        snapshot = InventorySnapshot.objects.get(store=self.store)
        self.assertEqual(snapshot.quantity, 9)
        self.assertEqual(snapshot.available, 7)


class AnalyticsMaintenanceApiTests(AnalyticsTestCase):
    @override_settings(CRON_SECRET="test-cron-secret-value")
    def test_cron_requires_bearer_secret_and_runs_both_tasks(self):
        old = StorefrontEvent.objects.create(
            store=self.store,
            event_type=StorefrontEvent.EventType.PAGE_VIEW,
            occurred_at=timezone.now() - timedelta(days=400),
        )
        url = reverse("analytics-maintenance")
        client = APIClient()

        unauthorized = client.get(url)
        response = client.get(url, HTTP_AUTHORIZATION="Bearer test-cron-secret-value")

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreaterEqual(response.json()["inventory_snapshots"], 1)
        self.assertFalse(StorefrontEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(InventorySnapshot.objects.filter(store=self.store).exists())

    @override_settings(CRON_SECRET="")
    def test_cron_fails_closed_when_secret_is_not_configured(self):
        response = APIClient().get(reverse("analytics-maintenance"))

        self.assertEqual(response.status_code, 503)
