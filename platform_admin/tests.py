from datetime import timedelta
from decimal import Decimal
import random
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from catalog.models import Product, RetailType, StockVariant
from orders.models import Order
from store.models import Store, StoreMember
from storefront_factory.models import StorefrontRequest
from storefront_factory.deployment_state import record_deployment_state
from subscriptions.entitlements import resolve_entitlement
from subscriptions.models import EntitlementDefinition, PlanEntitlement, PlanRevision, SubscriptionPayment, SubscriptionPlan, UserSubscription
from platform_admin.staff_roles import GROUP_PREFIX
from platform_admin.models import PlatformCommerceSettings, PlatformMonitoringSettings, PlatformPresentation, UserAdministrationEvent
from payments.models import PlatformPaymentGateway

User = get_user_model()


def translated_plan_copy(name, description):
    return {
        "en": {"name": name, "description": description},
        "fr": {"name": f"{name} FR", "description": f"{description} FR"},
        "ar": {"name": f"{name} AR", "description": f"{description} AR"},
    }


class PlatformAdminAPITestsMixin:
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_superuser(email="ops@example.com", password="pass")
        cls.read_only_staff = User.objects.create_user(email="support@example.com", password="pass", is_staff=True)
        cls.merchant = User.objects.create_user(email="merchant@example.com", password="pass")
        retail_type = RetailType.objects.create(key="general-test", label="General test")
        cls.store = Store.objects.create(name="Merchant One", slug="merchant-one", retail_type=retail_type)
        StoreMember.objects.create(user=cls.merchant, store=cls.store, role=StoreMember.Roles.OWNER)

    def client_for(self, user=None):
        client = APIClient()
        if user:
            client.force_authenticate(user)
        return client


class PlatformAdminAuthorizationTests(PlatformAdminAPITestsMixin, TestCase):
    def test_owner_configures_free_trial_duration(self):
        route = reverse("platform-admin-commerce-settings")
        self.assertEqual(self.client_for().get(route).status_code, 401)
        denied = self.client_for(self.read_only_staff).patch(route, {"free_trial_days": 30}, format="json")
        self.assertEqual(denied.status_code, 403)
        invalid = self.client_for(self.staff).patch(route, {"free_trial_days": 0}, format="json")
        self.assertEqual(invalid.status_code, 400)
        response = self.client_for(self.staff).patch(route, {"free_trial_days": 21}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["free_trial_days"], 21)
        self.assertEqual(PlatformCommerceSettings.load().free_trial_days, 21)

    @override_settings(
        PLATFORM_PAYMENT_GATEWAY="youcanpay",
        PLATFORM_PAYMENT_PRIVATE_KEY="test-private-key",
        PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=False,
    )
    def test_owner_can_enable_and_disable_configured_membership_gateway(self):
        listing = self.client_for(self.staff).get(reverse("platform-admin-payment-gateways"))
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(next(item for item in listing.data["gateways"] if item["code"] == "youcanpay")["enabled"])

        updated = self.client_for(self.staff).patch(
            reverse("platform-admin-payment-gateway-detail", args=["youcanpay"]),
            {"enabled": False}, format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.data["enabled"])
        self.assertFalse(PlatformPaymentGateway.objects.get(code="youcanpay").is_enabled)

        sandbox = self.client_for(self.staff).patch(
            reverse("platform-admin-payment-gateway-detail", args=["youcanpay"]),
            {"sandbox_enabled": False}, format="json",
        )
        self.assertEqual(sandbox.status_code, 200)
        self.assertFalse(sandbox.data["sandbox"])

    @override_settings(
        PAYPAL_CLIENT_ID="sandbox-client",
        PAYPAL_CLIENT_SECRET="sandbox-secret",
        PAYPAL_RECEIVER_EMAIL="sandbox@example.test",
        PAYPAL_LIVE_CLIENT_ID="live-client",
        PAYPAL_LIVE_CLIENT_SECRET="live-secret",
        PAYPAL_LIVE_RECEIVER_EMAIL="live@example.test",
    )
    def test_owner_can_switch_paypal_between_sandbox_and_live(self):
        PlatformPaymentGateway.objects.create(
            code="paypal", is_enabled=True, sandbox_enabled=True,
        )
        updated = self.client_for(self.staff).patch(
            reverse("platform-admin-payment-gateway-detail", args=["paypal"]),
            {"sandbox_enabled": False}, format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.data["sandbox"])
        self.assertTrue(updated.data["sandbox_configurable"])
        self.assertTrue(updated.data["configured"])
        self.assertFalse(PlatformPaymentGateway.objects.get(code="paypal").sandbox_enabled)

    def test_showroom_presentation_is_public_but_only_owner_can_change_it(self):
        public_route = reverse("platform-presentation-public")
        admin_route = reverse("platform-admin-presentation")
        self.assertEqual(self.client_for().get(public_route).status_code, 200)
        self.assertEqual(self.client_for().patch(admin_route, {}, format="json").status_code, 401)
        self.assertEqual(self.client_for(self.read_only_staff).patch(admin_route, {}, format="json").status_code, 403)

        response = self.client_for(self.staff).patch(admin_route, {
            "contact_email": "hello@tijarago.com",
            "support_phone": "+212 500 000 000",
            "whatsapp_phone": "+212 600 000 000",
            "social_links": {"instagram": "https://instagram.com/tijarago"},
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlatformPresentation.load().contact_email, "hello@tijarago.com")
        self.assertEqual(self.client_for().get(public_route).data["social_links"]["instagram"], "https://instagram.com/tijarago")

    def test_showroom_presentation_rejects_unknown_or_insecure_social_links(self):
        route = reverse("platform-admin-presentation")
        unknown = self.client_for(self.staff).patch(route, {
            "social_links": {"snapchat": "https://snapchat.com/tijarago"},
        }, format="json")
        insecure = self.client_for(self.staff).patch(route, {
            "social_links": {"instagram": "http://instagram.com/tijarago"},
        }, format="json")
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(insecure.status_code, 400)

    def test_monitoring_settings_default_to_service_mailbox_and_owner_controls_updates(self):
        route = reverse("platform-admin-monitoring")
        viewed = self.client_for(self.read_only_staff).get(route)
        self.assertEqual(viewed.status_code, 200)
        self.assertEqual(viewed.data["alert_email"], "tijarago.services@gmail.com")
        self.assertEqual(
            self.client_for(self.read_only_staff).patch(route, {"enabled": False}, format="json").status_code,
            403,
        )

        updated = self.client_for(self.staff).patch(route, {
            "alert_email": "alerts@example.test",
            "enabled": True,
            "reminder_minutes": 180,
        }, format="json")
        self.assertEqual(updated.status_code, 200)
        record = PlatformMonitoringSettings.load()
        self.assertEqual(record.alert_email, "alerts@example.test")
        self.assertEqual(record.reminder_minutes, 180)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="hello@example.test",
        PLATFORM_SUPPORT_EMAIL="support@example.test",
    )
    def test_owner_can_send_monitoring_test_email(self):
        from django.core import mail

        response = self.client_for(self.staff).post(reverse("platform-admin-monitoring-test"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox[-1].to, ["tijarago.services@gmail.com"])
        self.assertIn("monitoring test", mail.outbox[-1].subject.lower())

    @override_settings(CRON_SECRET="cron-test-secret")
    @patch("platform_admin.views.dispatch_monitoring_alert")
    def test_monitoring_cron_requires_secret_and_dispatches(self, dispatch):
        dispatch.return_value = {"status": "healthy", "delivered": False, "incidents": 0}
        route = reverse("platform-monitoring-cron")
        self.assertEqual(self.client_for().get(route).status_code, 401)
        response = self.client_for().get(route, HTTP_AUTHORIZATION="Bearer cron-test-secret")
        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once_with()

    @override_settings(CRON_SECRET="cron-test-secret")
    @patch("subscriptions.lifecycle.dispatch_subscription_lifecycle_notices")
    def test_subscription_lifecycle_cron_requires_secret_and_dispatches(self, dispatch):
        dispatch.return_value = {"sent": 2}
        route = reverse("subscription-lifecycle-cron")
        self.assertEqual(self.client_for().get(route).status_code, 401)
        response = self.client_for().get(route, HTTP_AUTHORIZATION="Bearer cron-test-secret")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"sent": 2})
        dispatch.assert_called_once_with()

    def test_anonymous_cannot_open_admin_api(self):
        response = self.client_for().get(reverse("platform-admin-overview"))
        self.assertEqual(response.status_code, 401)

    def test_merchant_cannot_open_admin_api(self):
        response = self.client_for(self.merchant).get(reverse("platform-admin-overview"))
        self.assertEqual(response.status_code, 403)

    def test_only_staff_can_change_pricing(self):
        payload = {"name": "Growth", "slug": "growth", "price": "149.00", "features": []}
        self.assertEqual(
            self.client_for().post(reverse("platform-admin-pricing"), payload, format="json").status_code,
            401,
        )
        self.assertEqual(
            self.client_for(self.merchant).post(
                reverse("platform-admin-pricing"), payload, format="json"
            ).status_code,
            403,
        )

    def test_staff_can_open_admin_api(self):
        response = self.client_for(self.staff).get(reverse("platform-admin-overview"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["metrics"]["stores"], 1)

    def test_read_only_staff_cannot_change_pricing(self):
        response = self.client_for(self.read_only_staff).post(
            reverse("platform-admin-pricing"),
            {"name": "Forbidden", "slug": "forbidden", "price": "50.00", "features": []},
            format="json",
        )

        self.assertEqual(response.status_code, 403)

    def test_staff_login_cookie_opens_admin_api(self):
        client = APIClient()
        login = client.post(
            "/accounts/token",
            {"email": self.staff.email, "password": "pass", "scope": "staff"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)

        response = client.get(reverse("platform-admin-session"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"]["email"], self.staff.email)

    def test_merchant_cannot_request_staff_login_scope(self):
        client = APIClient()
        response = client.post(
            "/accounts/token",
            {"email": self.merchant.email, "password": "pass", "scope": "staff"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)
        invalid = client.post(
            "/accounts/token",
            {"email": self.merchant.email, "password": "incorrect", "scope": "staff"},
            format="json",
        )
        self.assertEqual(response.data, invalid.data)


class PlatformStaffManagementTests(PlatformAdminAPITestsMixin, TestCase):
    def test_only_platform_owner_can_open_staff_management(self):
        route = reverse("platform-admin-staff")
        self.assertEqual(self.client_for().get(route).status_code, 401)
        self.assertEqual(self.client_for(self.merchant).get(route).status_code, 403)
        self.assertEqual(self.client_for(self.read_only_staff).get(route).status_code, 403)

        response = self.client_for(self.staff).get(route)
        self.assertEqual(response.status_code, 200)
        self.assertIn("platform_owner", {role["slug"] for role in response.data["roles"]})
        self.assertIn("merchant_support", {role["slug"] for role in response.data["roles"]})
        self.assertIn(
            "subscriptions.change_subscriptionplan",
            {permission["code"] for permission in response.data["permissions"]},
        )

    def test_owner_creates_dedicated_staff_with_curated_role(self):
        response = self.client_for(self.staff).post(
            reverse("platform-admin-staff"),
            {
                "email": "billing.team@example.com",
                "first_name": "Billing",
                "last_name": "Team",
                "password": "Strong-Staff-Password-2026!",
                "role": "billing_manager",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        operator = User.objects.get(email="billing.team@example.com")
        self.assertTrue(operator.is_staff)
        self.assertTrue(operator.is_active)
        self.assertFalse(hasattr(operator, "merchant_profile"))
        self.assertFalse(hasattr(operator, "client_profile"))
        self.assertFalse(operator.store_memberships.exists())
        self.assertTrue(operator.groups.filter(name=f"{GROUP_PREFIX}billing_manager").exists())
        self.assertTrue(operator.has_perm("subscriptions.change_subscriptionplan"))
        self.assertFalse(operator.has_perm("orders.change_order"))

    def test_existing_merchant_identity_cannot_be_promoted_to_staff(self):
        response = self.client_for(self.staff).post(
            reverse("platform-admin-staff"),
            {
                "email": self.merchant.email,
                "password": "Strong-Staff-Password-2026!",
                "role": "merchant_support",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.merchant.refresh_from_db()
        self.assertFalse(self.merchant.is_staff)

    def test_role_permissions_control_backend_areas_and_navigation_access(self):
        created = self.client_for(self.staff).post(
            reverse("platform-admin-staff"),
            {
                "email": "delivery@example.com",
                "password": "Strong-Staff-Password-2026!",
                "role": "delivery_specialist",
            },
            format="json",
        )
        operator = User.objects.get(pk=created.data["id"])
        client = self.client_for(operator)

        self.assertEqual(client.get(reverse("platform-admin-stores")).status_code, 200)
        self.assertEqual(client.get(reverse("platform-admin-deployments")).status_code, 200)
        self.assertEqual(client.get(reverse("platform-admin-orders")).status_code, 403)
        self.assertEqual(client.get(reverse("platform-admin-pricing")).status_code, 403)
        self.assertEqual(client.get(reverse("platform-admin-membership-payments")).status_code, 403)
        session = client.get(reverse("platform-admin-session"))
        self.assertTrue(session.data["user"]["access"]["stores"])
        self.assertTrue(session.data["user"]["access"]["deployments"])
        self.assertFalse(session.data["user"]["access"]["orders"])
        self.assertFalse(session.data["user"]["access"]["staff"])

    def test_owner_can_change_role_and_suspend_staff_but_not_edit_owner(self):
        operator = User.objects.create_user(
            email="operator@example.com", password="Strong-Staff-Password-2026!", is_staff=True,
        )
        route = reverse("platform-admin-staff-detail", args=[operator.pk])
        response = self.client_for(self.staff).patch(
            route, {"role": "analyst", "is_active": False}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        operator.refresh_from_db()
        self.assertFalse(operator.is_active)
        analyst_group = operator.groups.get(name=f"{GROUP_PREFIX}analyst")
        self.assertTrue(analyst_group.permissions.filter(
            content_type__app_label="orders", codename="view_order",
        ).exists())
        self.assertFalse(operator.has_perm("orders.change_order"))
        owner_response = self.client_for(self.staff).patch(
            reverse("platform-admin-staff-detail", args=[self.staff.pk]),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(owner_response.status_code, 409)


class PlatformUserManagementTests(PlatformAdminAPITestsMixin, TestCase):
    def test_superuser_can_update_identity_status_and_verification(self):
        account = User.objects.create_user(email="customer@example.com", password="pass")
        response = self.client_for(self.staff).patch(
            reverse("platform-admin-user-detail", args=[account.pk]),
            {
                "email": "renamed@example.com",
                "first_name": "New",
                "last_name": "Name",
                "is_active": False,
                "email_verified": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        account.refresh_from_db()
        self.assertEqual(account.email, "renamed@example.com")
        self.assertEqual(account.first_name, "New")
        self.assertFalse(account.is_active)
        self.assertIsNotNone(account.email_verified_at)
        event = UserAdministrationEvent.objects.get(target=account, action="updated")
        self.assertEqual(event.actor, self.staff)
        self.assertIn("email", event.changes)

    def test_non_superuser_cannot_modify_users(self):
        account = User.objects.create_user(email="customer@example.com", password="pass")
        response = self.client_for(self.read_only_staff).patch(
            reverse("platform-admin-user-detail", args=[account.pk]),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_delete_safe_account_and_audit_is_retained(self):
        account = User.objects.create_user(email="remove@example.com", password="pass")
        account_id = account.pk
        response = self.client_for(self.staff).delete(
            reverse("platform-admin-user-detail", args=[account_id]),
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(User.objects.filter(pk=account_id).exists())
        event = UserAdministrationEvent.objects.get(target_user_id=account_id, action="deleted")
        self.assertIsNone(event.target)
        self.assertEqual(event.target_email, "remove@example.com")

    def test_self_and_store_owner_deletion_are_blocked(self):
        self.assertEqual(
            self.client_for(self.staff).delete(
                reverse("platform-admin-user-detail", args=[self.staff.pk]),
            ).status_code,
            409,
        )
        response = self.client_for(self.staff).delete(
            reverse("platform-admin-user-detail", args=[self.merchant.pk]),
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("store_owner", response.data["blockers"])
        self.assertTrue(User.objects.filter(pk=self.merchant.pk).exists())

    def test_user_list_exposes_management_state(self):
        response = self.client_for(self.staff).get(reverse("platform-admin-users"))
        self.assertEqual(response.status_code, 200)
        owner = next(item for item in response.data["results"] if item["id"] == self.merchant.pk)
        self.assertFalse(owner["can_delete"])
        self.assertIn("store_owner", owner["delete_blockers"])
        self.assertIn("email_verified", owner)


class PlatformAdminOverviewTests(PlatformAdminAPITestsMixin, TestCase):
    def test_all_monitoring_resources_respond_for_staff(self):
        for route in (
            "platform-admin-users",
            "platform-admin-orders",
            "platform-admin-payments",
            "platform-admin-membership-payments",
            "platform-admin-deployments",
            "platform-admin-system",
            "platform-admin-pricing",
        ):
            with self.subTest(route=route):
                response = self.client_for(self.staff).get(reverse(route))
                self.assertEqual(response.status_code, 200)

    def test_membership_ledger_is_distinct_from_store_customer_payments(self):
        plan = SubscriptionPlan.objects.get(slug="free")
        payment = SubscriptionPayment.objects.create(
            user=self.merchant,
            plan=plan,
            plan_name="Starter",
            amount=Decimal("79.00"),
            currency="MAD",
            duration_days=30,
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            status=SubscriptionPayment.Status.CONFIRMED,
            provider_transaction_id="membership-provider-ref",
        )

        response = self.client_for(self.staff).get(
            reverse("platform-admin-membership-payments"),
            {"search": self.merchant.email, "status": "confirmed"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], str(payment.pk))
        self.assertEqual(response.data["results"][0]["merchant_email"], self.merchant.email)

    def test_overview_aggregates_platform_activity(self):
        order = Order.objects.create(
            store=self.store,
            amount=Decimal("125.50"),
            currency="MAD",
            payment_mode=Order.PaymentMode.COD,
            is_paid=Order.IsPaid.COD,
            status=Order.Status.DELIVERED,
        )
        StorefrontRequest.objects.create(
            store=self.store,
            name=self.store.name,
            slug=self.store.slug,
            design="basic",
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
        )

        response = self.client_for(self.staff).get(reverse("platform-admin-overview"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["metrics"]["orders"], 1)
        self.assertEqual(response.data["metrics"]["realized_revenue"], 125.5)
        self.assertEqual(response.data["metrics"]["deployed_storefronts"], 1)
        self.assertEqual(response.data["recent"]["orders"][0]["order_id"], str(order.order_id))
        self.assertEqual(len(response.data["trend"]), 14)

    def test_system_reports_unstocked_active_products(self):
        section = self.store.catalog_sections.create(key="products", label="Products")
        Product.objects.create(store=self.store, section=section, name="No stock", ref="nostock")
        stocked = Product.objects.create(store=self.store, section=section, name="Stocked", ref="stocked")
        StockVariant.objects.create(store=self.store, product=stocked, quantity=2)

        response = self.client_for(self.staff).get(reverse("platform-admin-system"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["catalog"]["active_without_variants"], 1)
        self.assertEqual(response.data["database"]["status"], "healthy")
        self.assertEqual(response.data["status"], "attention")
        self.assertGreaterEqual(response.data["attention_count"], 1)

    def test_store_search_returns_only_matching_store(self):
        Store.objects.create(name="Second merchant", slug="second-merchant")

        response = self.client_for(self.staff).get(
            reverse("platform-admin-stores"), {"search": "Merchant One"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["slug"], "merchant-one")


class PlatformMonitoringDeliveryTests(TestCase):
    @patch("platform_admin.monitoring.send_platform_email", return_value=True)
    @patch("platform_admin.monitoring.collect_operational_incidents")
    def test_alerts_are_deduplicated_and_recovery_is_sent_once(self, collect, send):
        from platform_admin.monitoring import dispatch_monitoring_alert

        now = timezone.now()
        collect.return_value = [{"key": "failed_deployments", "label": "Failed deployments", "count": 2}]
        first = dispatch_monitoring_alert(now)
        duplicate = dispatch_monitoring_alert(now + timedelta(minutes=10))
        collect.return_value = []
        recovered = dispatch_monitoring_alert(now + timedelta(minutes=11))
        healthy = dispatch_monitoring_alert(now + timedelta(minutes=12))

        self.assertTrue(first["delivered"])
        self.assertEqual(duplicate["status"], "unchanged")
        self.assertTrue(recovered["delivered"])
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(send.call_count, 2)

    @patch("platform_admin.monitoring.send_platform_email", return_value=True)
    @patch("platform_admin.monitoring.collect_operational_incidents")
    def test_unresolved_alert_repeats_after_configured_interval(self, collect, send):
        from platform_admin.monitoring import dispatch_monitoring_alert

        record = PlatformMonitoringSettings.objects.create(reminder_minutes=30)
        collect.return_value = [{"key": "failed_payments", "label": "Failed payments", "count": 1}]
        now = timezone.now()
        dispatch_monitoring_alert(now)
        dispatch_monitoring_alert(now + timedelta(minutes=29))
        repeated = dispatch_monitoring_alert(now + timedelta(minutes=30))

        record.refresh_from_db()
        self.assertTrue(repeated["delivered"])
        self.assertEqual(record.last_alert_sent_at, now + timedelta(minutes=30))
        self.assertEqual(send.call_count, 2)


class StorefrontReleaseManagementTests(PlatformAdminAPITestsMixin, TestCase):
    def live_deployment(self, **overrides):
        values = {
            "store": self.store,
            "name": self.store.name,
            "slug": self.store.slug,
            "design": "basic",
            "status": StorefrontRequest.STATUS_CREATED,
            "deploy_status": StorefrontRequest.DEPLOY_DEPLOYED,
            "pipeline_state": StorefrontRequest.PIPELINE_LIVE,
            "deployed_url": f"https://{self.store.slug}.example.com",
            "vercel_project_id": "prj_store",
            "vercel_deployment_id": "dpl_current",
            "github_commit_sha": "c" * 40,
            "target_revision": "a" * 40,
            "deployed_revision": "a" * 40,
            "last_known_good_revision": "a" * 40,
            "last_known_good_deployment_id": "dpl_current",
        }
        values.update(overrides)
        return StorefrontRequest.objects.create(**values)

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    @patch("platform_admin.views.enqueue_storefront_deployment")
    def test_owner_can_queue_one_update_without_hiding_live_store(self, enqueue):
        deployment = self.live_deployment()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.staff).post(
                reverse("platform-admin-deployment-release", args=[deployment.pk]),
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.operation, StorefrontRequest.OPERATION_UPDATE)
        self.assertEqual(deployment.target_revision, "b" * 40)
        self.assertEqual(deployment.deployed_revision, "a" * 40)
        self.assertEqual(deployment.last_known_good_revision, "a" * 40)
        self.assertEqual(deployment.last_known_good_deployment_id, "dpl_current")
        self.assertEqual(deployment.deployed_url, f"https://{self.store.slug}.example.com")
        self.assertEqual(deployment.vercel_deployment_id, "")
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        self.assertEqual(response.data["release_status"], "updating")
        enqueue.assert_called_once_with(deployment.pk, force=True, oidc_token="")

    @override_settings(STOREFRONT_RELEASE_REVISION="a" * 40)
    @patch("platform_admin.views.enqueue_storefront_deployment")
    def test_current_storefront_cannot_be_redeployed(self, enqueue):
        deployment = self.live_deployment()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.staff).post(
                reverse("platform-admin-deployment-release", args=[deployment.pk]),
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["code"], "storefront_already_current")
        deployment.refresh_from_db()
        self.assertEqual(deployment.target_revision, "a" * 40)
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_LIVE)
        enqueue.assert_not_called()

    def test_staff_can_stream_deployment_progress_but_merchant_cannot(self):
        deployment = self.live_deployment()
        record_deployment_state(deployment.pk, StorefrontRequest.PIPELINE_BUILDING, message="deployment.building")

        response = self.client_for(self.staff).get(
            reverse("platform-admin-deployment-events", args=[deployment.pk]) + "?once=1",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content)
        self.assertIn(b"event: deployment.updated", body)
        self.assertIn(b'"state":"building"', body)

        forbidden = self.client_for(self.merchant).get(
            reverse("platform-admin-deployment-events", args=[deployment.pk]) + "?once=1",
            HTTP_ACCEPT="text/event-stream",
        )
        self.assertEqual(forbidden.status_code, 403)

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    def test_live_storefront_without_recorded_revision_requires_update(self):
        deployment = self.live_deployment(
            target_revision="",
            deployed_revision="",
            last_known_good_revision="",
        )

        response = self.client_for(self.staff).get(reverse("platform-admin-deployments"))

        self.assertEqual(response.status_code, 200)
        result = next(item for item in response.data["results"] if item["id"] == deployment.pk)
        self.assertEqual(result["release_status"], "update_available")

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    @patch("platform_admin.views.enqueue_storefront_deployment")
    def test_live_storefront_without_recorded_revision_can_queue_update(self, enqueue):
        deployment = self.live_deployment(
            target_revision="",
            deployed_revision="",
            last_known_good_revision="",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.staff).post(
                reverse("platform-admin-deployment-release", args=[deployment.pk]),
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.target_revision, "b" * 40)
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        self.assertEqual(response.data["release_status"], "updating")
        enqueue.assert_called_once_with(deployment.pk, force=True, oidc_token="")

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    @patch("platform_admin.views.enqueue_storefront_deployment")
    def test_retired_design_requires_explicit_redesign_before_update(self, enqueue):
        deployment = self.live_deployment(design="nova")

        response = self.client_for(self.staff).post(
            reverse("platform-admin-deployment-release", args=[deployment.pk]),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "storefront_redesign_required")
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_LIVE)
        enqueue.assert_not_called()

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    @patch("platform_admin.views.enqueue_storefront_deployment")
    def test_failed_update_can_be_retried_while_previous_release_stays_live(self, enqueue):
        deployment = self.live_deployment(
            status=StorefrontRequest.STATUS_FAILED,
            deploy_status=StorefrontRequest.DEPLOY_FAILED,
            pipeline_state=StorefrontRequest.PIPELINE_FAILED,
            operation=StorefrontRequest.OPERATION_UPDATE,
            target_revision="b" * 40,
            vercel_deployment_id="",
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.staff).post(
                reverse("platform-admin-deployment-release", args=[deployment.pk]),
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 202, response.data)
        deployment.refresh_from_db()
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        self.assertEqual(response.data["release_status"], "updating")
        self.assertEqual(deployment.deployed_url, f"https://{self.store.slug}.example.com")
        self.assertEqual(deployment.last_known_good_revision, "a" * 40)
        enqueue.assert_called_once_with(deployment.pk, force=True, oidc_token="")

    @override_settings(STOREFRONT_RELEASE_REVISION="")
    def test_update_requires_an_approved_immutable_release(self):
        deployment = self.live_deployment()
        response = self.client_for(self.staff).post(
            reverse("platform-admin-deployment-release", args=[deployment.pk]),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "storefront_release_unavailable")

    @override_settings(STOREFRONT_RELEASE_REVISION="b" * 40)
    def test_merchant_cannot_queue_platform_release(self):
        deployment = self.live_deployment()
        response = self.client_for(self.merchant).post(
            reverse("platform-admin-deployment-release", args=[deployment.pk]),
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    @patch("storefront_factory.services.get_deployment_state")
    def test_deployment_list_does_not_wait_for_provider_reconciliation(self, provider_status):
        deployment = self.live_deployment(
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            pipeline_state=StorefrontRequest.PIPELINE_BUILDING,
        )

        response = self.client_for(self.staff).get(reverse("platform-admin-deployments"))

        self.assertEqual(response.status_code, 200)
        result = next(item for item in response.data["results"] if item["id"] == deployment.pk)
        self.assertEqual(result["pipeline_state"], StorefrontRequest.PIPELINE_BUILDING)
        self.assertEqual(result["progress"], deployment.progress)
        provider_status.assert_not_called()


class PremiumPricingTests(PlatformAdminAPITestsMixin, TestCase):
    @override_settings(
        PLATFORM_PAYMENT_PRIVATE_KEY="pri_sandbox_test",
        PLATFORM_PAYMENT_PUBLIC_KEY="pub_sandbox_test",
        PLATFORM_PAYMENT_ACCESS_TOKEN="oauth_sandbox_test",
        PLATFORM_PAYMENT_SANDBOX=True,
    )
    def test_pricing_response_reports_provider_without_exposing_credentials(self):
        response = self.client_for(self.staff).get(reverse("platform-admin-pricing"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual({plan["slug"] for plan in response.data["plans"]}, {"free", "premium"})
        self.assertTrue(response.data["provider"]["configured"])
        self.assertTrue(response.data["provider"]["sandbox"])
        capability_keys = {item["key"] for item in response.data["capabilities"]}
        rule_keys = {item["key"] for item in response.data["rule_templates"]}
        self.assertIn("designs.premium", capability_keys)
        self.assertIn("stores.max_count", capability_keys)
        self.assertEqual(capability_keys, rule_keys)
        self.assertTrue(all(set(item["translations"]) == {"en", "fr", "ar"} for item in response.data["capabilities"]))
        self.assertTrue(all(item["configured"] for item in response.data["rule_templates"]))
        plans = {item["slug"]: item for item in response.data["plans"]}
        self.assertIn("Stores included: 1 store", {item["display"] for item in plans["free"]["benefits"]})
        self.assertIn("Stores included: 3 stores", {item["display"] for item in plans["premium"]["benefits"]})
        rendered = str(response.data)
        self.assertNotIn("pri_sandbox_test", rendered)
        self.assertNotIn("pub_sandbox_test", rendered)

    def test_staff_can_save_randomized_valid_pricing_values(self):
        rng = random.Random(20260822)
        currencies = ["MAD", "EUR", "USD"]
        scopes = [choice for choice, _label in SubscriptionPlan.EntitlementScope.choices]

        for _index in range(8):
            payload = {
                "name": f"Pack {_index}",
                "slug": f"pack-{_index}",
                "translations": translated_plan_copy(f"Pack {_index}", f"Offer {_index}"),
                "price": f"{rng.randint(5, 1500)}.{rng.randint(0, 99):02d}",
                "currency": rng.choice(currencies),
                "duration_days": rng.randint(1, 730),
                "entitlement_scope": rng.choice(scopes),
                "is_active": rng.choice([True, False]),
                "features": [f"Feature {_index}", "Premium support"],
            }
            with self.subTest(payload=payload):
                response = self.client_for(self.staff).post(
                    reverse("platform-admin-pricing"), payload, format="json"
                )
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data["price"], payload["price"])
                self.assertEqual(response.data["currency"], payload["currency"])
                self.assertEqual(response.data["duration_days"], payload["duration_days"])
                self.assertEqual(response.data["entitlement_scope"], payload["entitlement_scope"])
                self.assertEqual(response.data["is_active"], payload["is_active"])

        self.assertEqual(SubscriptionPlan.objects.count(), 10)

    def test_pricing_rejects_invalid_values_without_partial_update(self):
        client = self.client_for(self.staff)
        premium = SubscriptionPlan.objects.get(slug="premium")
        route = reverse("platform-admin-plan-detail", args=[premium.id])
        valid = client.patch(
            route,
            {
                "price": "120.00",
                "currency": "MAD",
                "duration_days": 30,
                "entitlement_scope": "all_owned_stores",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(valid.status_code, 200)

        invalid_payloads = [
            {"price": "4.99"},
            {"currency": "MOROCCAN"},
            {"duration_days": 0},
            {"duration_days": 3651},
            {"entitlement_scope": "everywhere"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = client.patch(route, payload, format="json")
                self.assertEqual(response.status_code, 400)

        premium.refresh_from_db()
        self.assertEqual(premium.price, Decimal("120.00"))
        self.assertEqual(premium.currency, "MAD")
        self.assertEqual(premium.duration_days, 30)
        self.assertTrue(premium.is_active)

    def test_staff_can_add_and_remove_a_pack_but_not_free(self):
        response = self.client_for(self.staff).post(
            reverse("platform-admin-pricing"),
            {
                "name": "Annual", "slug": "annual", "price": "999.00", "currency": "MAD",
                "duration_days": 365, "features": ["Annual access"],
                "translations": translated_plan_copy("Annual", "Annual access"),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.client_for(self.staff).delete(reverse("platform-admin-plan-detail", args=[response.data["id"]])).status_code,
            204,
        )
        free = SubscriptionPlan.objects.get(slug="free")
        self.assertEqual(
            self.client_for(self.staff).delete(reverse("platform-admin-plan-detail", args=[free.id])).status_code,
            409,
        )

    def test_baseline_free_plan_cannot_be_made_paid_or_disabled(self):
        free = SubscriptionPlan.objects.get(slug="free")
        route = reverse("platform-admin-plan-detail", args=[free.id])
        for payload in ({"price": "5.00"}, {"is_active": False}, {"grants_premium_designs": True}, {"slug": "renamed"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client_for(self.staff).patch(route, payload, format="json").status_code, 400)

    def test_only_governed_benefits_are_accepted_and_plan_changes_create_revisions(self):
        rejected = self.client_for(self.staff).post(
            reverse("platform-admin-capabilities"),
            {
                "key": "catalog.products_limit",
                "name": "Product limit",
                "value_type": "integer",
                "scope": "store",
                "unit": "products",
                "default_value": 25,
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        capability = EntitlementDefinition.objects.get(key="stores.max_count")
        premium = SubscriptionPlan.objects.get(slug="premium")
        initial_revision = premium.revision

        invalid = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {"entitlements": {"stores.max_count": -1}},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400)

        updated = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {"entitlements": {"stores.max_count": 500}},
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["revision"], initial_revision + 1)
        self.assertEqual(updated.data["entitlements"]["stores.max_count"], 500)
        revision = PlanRevision.objects.get(plan=premium, revision=initial_revision + 1)
        self.assertEqual(revision.snapshot["entitlements"]["stores.max_count"], 500)

        self.assertTrue(PlanEntitlement.objects.filter(plan=premium, definition=capability).exists())
        blocked = self.client_for(self.staff).delete(
            reverse("platform-admin-capability-detail", args=[capability.id]),
        )
        self.assertEqual(blocked.status_code, 409)

    def test_partial_plan_edit_preserves_premium_access_when_rule_is_inactive(self):
        premium = SubscriptionPlan.objects.get(slug="premium")
        premium_rule = EntitlementDefinition.objects.get(key="designs.premium")
        self.assertTrue(premium.grants_premium_designs)
        EntitlementDefinition.objects.filter(pk=premium_rule.pk).update(is_active=False)

        response = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {
                "translations": translated_plan_copy("Premium", "Updated commercial wording"),
                # Simulates an older/partial frontend that cannot see the
                # inactive entitlement and derives a false legacy flag.
                "grants_premium_designs": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        premium.refresh_from_db()
        self.assertTrue(premium.grants_premium_designs)
        self.assertEqual(premium.description, "Updated commercial wording")

    def test_plan_copy_requires_complete_english_french_and_arabic_translations(self):
        response = self.client_for(self.staff).post(
            reverse("platform-admin-pricing"),
            {
                "name": "Incomplete",
                "slug": "incomplete",
                "description": "English only",
                "price": "99.00",
                "translations": {
                    "en": {"name": "Incomplete", "description": "English only"},
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("translations", response.data)

    def test_explicit_premium_entitlement_change_updates_legacy_cache(self):
        premium = SubscriptionPlan.objects.get(slug="premium")

        response = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {"entitlements": {"designs.premium": False}},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        premium.refresh_from_db()
        self.assertFalse(premium.grants_premium_designs)
        self.assertFalse(response.data["entitlements"]["designs.premium"])

    def test_plan_edit_can_apply_new_entitlements_to_active_subscribers(self):
        premium = SubscriptionPlan.objects.get(slug="premium")
        analytics = EntitlementDefinition.objects.get(key="analytics.level")
        subscriber = User.objects.create_user(email="advanced@example.com", password="pass")
        subscription = UserSubscription.objects.create(
            user=subscriber,
            plan=premium,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=20),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            plan_revision=premium.revision,
            entitlements_snapshot={analytics.key: "standard"},
        )

        response = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {
                "entitlements": {analytics.key: "advanced"},
                "subscriber_transition": "existing_and_new",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        subscription.refresh_from_db()
        self.assertEqual(subscription.entitlements_snapshot[analytics.key], "advanced")
        self.assertEqual(subscription.plan_revision, response.data["revision"])
        self.assertEqual(resolve_entitlement(subscriber, analytics.key), "advanced")

    def test_plan_edit_can_preserve_existing_subscriber_snapshot(self):
        premium = SubscriptionPlan.objects.get(slug="premium")
        analytics = EntitlementDefinition.objects.get(key="analytics.level")
        subscriber = User.objects.create_user(email="grandfathered@example.com", password="pass")
        original_revision = premium.revision
        subscription = UserSubscription.objects.create(
            user=subscriber,
            plan=premium,
            status=UserSubscription.Status.ACTIVE,
            current_period_end=timezone.now() + timedelta(days=20),
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            plan_revision=original_revision,
            entitlements_snapshot={analytics.key: "standard"},
        )

        response = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {
                "entitlements": {analytics.key: "advanced"},
                "subscriber_transition": "new_only",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        subscription.refresh_from_db()
        self.assertEqual(subscription.entitlements_snapshot[analytics.key], "standard")
        self.assertEqual(subscription.plan_revision, original_revision)

    def test_plan_edit_rejects_unknown_subscriber_transition(self):
        premium = SubscriptionPlan.objects.get(slug="premium")

        response = self.client_for(self.staff).patch(
            reverse("platform-admin-plan-detail", args=[premium.id]),
            {"subscriber_transition": "silently_replace_everything"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("subscriber_transition", response.data)
