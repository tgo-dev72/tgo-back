"""Golden merchant journey spanning identity, billing, store, catalog and orders.

External email, payment and deployment providers are isolated; every internal
API boundary and persisted transition is real.  This is intentionally broader
than the focused app tests: it protects the customer-visible path from contract
drift between otherwise healthy components.
"""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.customer_email_verification import make_customer_email_verification_token
from accounts.email_verification import make_email_verification_token
from accounts.models import AuthUser
from catalog.models import Product, StockVariant
from catalog.seeds import ensure_retail_types
from marketing.models import Subscriber
from orders.models import Order
from store.models import Store, StoreCustomer, StorefrontPage
from storefront_factory.models import StorefrontRequest
from subscriptions.models import SubscriptionPayment, SubscriptionPlan, UserSubscription


class _FakePlatformPaymentClient:
    """Minimal external checkout returned by the gateway adapter."""

    provider_token = "tok_golden_journey"
    payment_url = "https://youcanpay.com/sandbox/payment-form/tok_golden_journey?lang=en"


@override_settings(
    ALLOWED_HOSTS=["*"],
    STOREFRONT_RELEASE_REVISION="",
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PLATFORM_PAYMENT_PRIVATE_KEY="golden_private_key",
    PLATFORM_PAYMENT_PUBLIC_KEY="golden_public_key",
    PLATFORM_PAYMENT_ACCESS_TOKEN="golden_access_token",
    PLATFORM_PAYMENT_SANDBOX=True,
    PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION=False,
    MERCHANT_DASHBOARD_URL="https://dash.example.test",
    EMAIL_VERIFICATION_FRONTEND_URL="https://dash.example.test/verify-email",
    CORS_ALLOWED_ORIGINS=["https://dash.example.test"],
    CSRF_TRUSTED_ORIGINS=["https://dash.example.test"],
    STOREFRONT_BASE_DOMAIN="example.test",
    STOREFRONT_API_URL="https://api.example.test",
)
class GoldenMerchantJourneyTests(TestCase):
    merchant_email = "merchant-journey@example.test"
    merchant_password = "Journey-pass-123"
    customer_email = "customer-journey@example.test"
    customer_password = "Customer-pass-123"

    def setUp(self):
        # API throttle counters live outside the transactional test database.
        # Keep this end-to-end journey independent from whichever API tests ran
        # before it in the same process.
        cache.clear()

    @classmethod
    def setUpTestData(cls):
        ensure_retail_types()
        cls.retail = cls._general_retail_type()
        cls.plan = SubscriptionPlan.objects.get(slug="premium")
        cls.plan.name = "Premium Journey"
        cls.plan.price = Decimal("149.00")
        cls.plan.currency = "MAD"
        cls.plan.is_active = True
        cls.plan.grants_premium_designs = True
        cls.plan.save(update_fields=[
            "name", "price", "currency", "is_active", "grants_premium_designs", "updated_at",
        ])

    @staticmethod
    def _general_retail_type():
        from catalog.models import RetailType

        return RetailType.objects.get(key="general")

    def _register_verify_and_login_merchant(self):
        merchant = APIClient()
        payload = {
            "email": self.merchant_email,
            "confirm_email": self.merchant_email,
            "password": self.merchant_password,
            "confirm_password": self.merchant_password,
            "first_name": "Nadia",
            "last_name": "Merchant",
            "business_name": "Golden Market",
            "phone_number": "+212612345678",
            "preferred_language": "en",
            "terms_accepted": True,
        }
        with self.captureOnCommitCallbacks(execute=True):
            registered = merchant.post("/accounts/register", payload, format="json")
        self.assertEqual(registered.status_code, 201, registered.data)
        self.assertTrue(registered.data["verification_required"])
        self.assertEqual(mail.outbox[-1].to, [self.merchant_email])

        user = AuthUser.objects.get(email=self.merchant_email)
        verified = merchant.post(
            "/accounts/verify-email",
            {"token": make_email_verification_token(user)},
            format="json",
        )
        self.assertEqual(verified.status_code, 200, verified.data)

        logged_in = merchant.post(
            "/accounts/token",
            {"email": self.merchant_email, "password": self.merchant_password, "scope": "owner"},
            format="json",
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.data)
        self.assertEqual(logged_in.data["session_scope"], "merchant")
        self.assertEqual(merchant.get("/accounts/me").status_code, 200)
        return merchant, user

    def _upgrade_to_premium(self, merchant, user):
        fake_client = _FakePlatformPaymentClient()
        idempotency_key = str(uuid.uuid4())
        with patch(
            "payments.gateways.youcanpay.YouCanPayGateway._create_external_checkout",
            return_value=fake_client,
        ):
            checkout = merchant.post(
                reverse("subscription-checkout"),
                {
                    "plan_id": self.plan.pk,
                    "lang": "en",
                    "return_origin": "https://dash.example.test",
                    "use_balance": False,
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY=idempotency_key,
            )
        self.assertEqual(checkout.status_code, 201, checkout.data)
        self.assertEqual(checkout.data["amount"], "149.00")

        payment = SubscriptionPayment.objects.get(pk=checkout.data["payment_id"])
        self.assertEqual(
            checkout.data["payment_url"],
            "https://youcanpay.com/sandbox/payment-form/tok_golden_journey?lang=en",
        )
        self.assertNotIn(str(payment.pk), checkout.data["payment_url"])
        self.assertEqual(str(payment.idempotency_key), idempotency_key)
        payload = {
            "id": f"evt_{payment.pk}",
            "event_name": "transaction.paid",
            "sandbox": True,
            "payload": {
                "token": {"id": payment.provider_token},
                "metadata": {
                    "purpose": "platform_subscription",
                    "payment_id": str(payment.pk),
                },
                "transaction": {
                    "id": f"txn_{payment.pk}",
                    "order_id": str(payment.pk),
                    "amount": int(payment.amount * 100),
                    "currency": payment.currency,
                    "status": 1,
                },
            },
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        signature = hmac.new(b"golden_private_key", raw, hashlib.sha256).hexdigest()
        transaction = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "id": f"txn_{payment.pk}",
                "status": "paid",
                "is_live_mode": False,
                "amount": {"amount": str(payment.amount), "currency": payment.currency},
                "order_id": str(payment.pk),
                "metadata": {
                    "purpose": "platform_subscription",
                    "payment_id": str(payment.pk),
                },
            },
        )
        with patch("requests.get", return_value=transaction):
            with patch("notifications.platform_email.send_subscription_activated") as notification:
                with self.captureOnCommitCallbacks(execute=True):
                    paid = APIClient().post(
                        reverse("subscription-provider-webhook", args=[payment.provider]),
                        data=raw,
                        content_type="application/json",
                        HTTP_X_YOUCANPAY_SIGNATURE=signature,
                    )
                notification.assert_called_once()
        self.assertEqual(paid.status_code, 200, paid.data)

        payment.refresh_from_db()
        self.assertEqual(payment.status, SubscriptionPayment.Status.CONFIRMED)
        subscription = UserSubscription.objects.get(user=user, status=UserSubscription.Status.ACTIVE)
        self.assertEqual(subscription.plan_id, self.plan.pk)
        status_response = merchant.get(reverse("subscription-status"))
        self.assertEqual(status_response.status_code, 200, status_response.data)
        self.assertEqual(status_response.data["subscription"]["plan"]["slug"], self.plan.slug)

    def _create_premium_store(self, merchant):
        response = merchant.post(
            reverse("store-list"),
            {
                "name": "Golden Store",
                "slug": "golden-store",
                "retail_type": self.retail.pk,
                "config": {
                    "design": "fashion",
                    "contact_email": "support@golden.example",
                    "support_phone": "+212612345678",
                    "whatsapp_phone": "+212612345678",
                    "social_links": {"instagram": "https://instagram.com/golden"},
                    "homepage_product_source": "featured",
                    "homepage_product_limit": 8,
                    "brand_color": "#8B6F47",
                    "currency": "MAD",
                },
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["storefront_status"], "draft")
        self.assertIsNotNone(response.data["storefront_request_id"])

        with patch("api.views.enqueue_storefront_deployment") as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                started = merchant.post(
                    f"/api/requests/{response.data['storefront_request_id']}/scaffold",
                    {},
                    format="json",
                )
            enqueue.assert_called_once()
        self.assertEqual(started.status_code, 202, started.data)

        store = Store.objects.select_related("config").get(pk=response.data["id"])
        self.assertEqual(store.config.plan, "premium")
        self.assertEqual(store.config.design, "fashion")
        self.assertEqual(store.config.social_links["instagram"], "https://instagram.com/golden")
        self.assertTrue(
            StorefrontPage.objects.filter(
                store=store,
                is_visible=True,
            ).exists()
        )
        self.assertTrue(
            all(page.published_sections for page in store.storefront_pages.all())
        )
        deployment = StorefrontRequest.objects.get(pk=response.data["storefront_request_id"])
        self.assertEqual(deployment.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)
        return store

    def _create_inventory(self, merchant, store):
        sections = merchant.get(f"/api/catalog/store/{store.pk}/sections/")
        self.assertEqual(sections.status_code, 200, sections.data)
        section = next(item for item in sections.data if item["key"] == "products")

        created = merchant.post(
            f"/api/catalog/store/{store.pk}/products/",
            {
                "section": section["id"],
                "name": "Golden Product",
                "ref": "GOLD-001",
                "description": "A product used by the complete merchant journey.",
                "price": "120.00",
                "promo": 0,
                "loyalty_points": 5,
                "extra": {"sku": "GOLD-001", "weight": 1, "weight_unit": "kg"},
                "attributes": {"brand": "Golden"},
                "is_active": True,
                "is_featured": True,
                "merchandising_order": 1,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        product = Product.objects.get(pk=created.data["id"])

        inventory = merchant.post(
            f"/api/catalog/store/{store.pk}/products/{product.pk}/variants/",
            {
                "attributes": {"option": "Standard"},
                "price": None,
                "quantity": 5,
                "extra": {"sku": "GOLD-001-STANDARD"},
            },
            format="json",
        )
        self.assertEqual(inventory.status_code, 201, inventory.data)
        variant = StockVariant.objects.get(pk=inventory.data["id"])

        public_config = APIClient().get(
            "/api/storefront/config/", HTTP_X_STORE_SLUG=store.slug,
        )
        self.assertEqual(public_config.status_code, 200, public_config.data)
        self.assertEqual(public_config.data["design"], "fashion")
        public_catalog = APIClient().get(
            "/api/catalog/products/", HTTP_X_STORE_SLUG=store.slug,
        )
        self.assertEqual(public_catalog.status_code, 200, public_catalog.data)
        self.assertEqual(public_catalog.data[0]["id"], product.pk)
        self.assertEqual(public_catalog.data[0]["variants"][0]["available_quantity"], 5)
        return product, variant

    def _register_verified_customer(self, store):
        customer = APIClient()
        headers = {"HTTP_X_STORE_SLUG": store.slug}
        payload = {
            "firstName": "Amina",
            "lastName": "Customer",
            "email": self.customer_email,
            "confirmEmail": self.customer_email,
            "phone": "+212623456789",
            "password": self.customer_password,
            "confirmPassword": self.customer_password,
            "preferredLanguage": "en",
        }
        with self.captureOnCommitCallbacks(execute=True):
            registered = customer.post("/api/auth/register/", payload, format="json", **headers)
        self.assertEqual(registered.status_code, 201, registered.data)
        self.assertTrue(registered.data["verificationRequired"])

        user = AuthUser.objects.get(email=self.customer_email)
        verified = customer.post(
            "/api/auth/verify-email/",
            {"token": make_customer_email_verification_token(user, store)},
            format="json",
            **headers,
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertTrue(verified.data["customer"]["emailVerified"])
        self.assertEqual(customer.get("/api/auth/me/", **headers).status_code, 200)
        return customer, headers

    def _complete_order(self, merchant, customer, customer_headers, store, product, variant):
        placed = customer.post(
            reverse("order-create"),
            {
                "payment_mode": "cod",
                "customer": {
                    "name": "Amina Customer",
                    "email": self.customer_email,
                    "phone": "+212623456789",
                    "city": "Casablanca",
                    "address": "Golden Street 1",
                },
                "items": [{"product_id": product.pk, "variant_id": variant.pk, "quantity": 1}],
                "marketing_consent": True,
            },
            format="json",
            **customer_headers,
        )
        self.assertEqual(placed.status_code, 201, placed.data)
        self.assertEqual(placed.data["status"], "pending")
        self.assertEqual(placed.data["is_paid"], "cod")
        order = Order.objects.get(order_id=placed.data["order_id"])
        self.assertIsNotNone(order.client_id)
        self.assertTrue(Subscriber.objects.filter(
            store=store, email=self.customer_email, status=Subscriber.Status.ACTIVE,
        ).exists())

        line_ids = [item["id"] for item in placed.data["items"]]
        confirmed = merchant.post(
            reverse("order-action", args=[store.pk, "confirm"]),
            {"order_id": str(order.order_id), "acknowledged_item_ids": line_ids},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], "confirmed")

        shipped = merchant.post(
            reverse("order-action", args=[store.pk, "ship"]),
            {
                "order_id": str(order.order_id),
                "carrier": "Golden Delivery",
                "tracking_number": "TRACK-001",
            },
            format="json",
        )
        self.assertEqual(shipped.status_code, 200, shipped.data)
        self.assertEqual(shipped.data["status"], "shipped")

        delivered = merchant.post(
            reverse("order-action", args=[store.pk, "deliver"]),
            {"order_id": str(order.order_id)},
            format="json",
        )
        self.assertEqual(delivered.status_code, 200, delivered.data)
        self.assertEqual(delivered.data["status"], "delivered")

        mine = customer.get(reverse("customer-orders"), **customer_headers)
        self.assertEqual(mine.status_code, 200, mine.data)
        self.assertEqual(mine.data[0]["status"], "delivered")
        self.assertEqual(mine.data[0]["tracking_number"], "TRACK-001")

        variant.refresh_from_db()
        self.assertEqual(variant.quantity, 4)
        self.assertEqual(variant.reserved, 0)
        store_customer = StoreCustomer.objects.get(store=store, profile__user__email=self.customer_email)
        self.assertEqual(store_customer.loyalty_points, 5)
        self.assertEqual(
            list(order.status_history.values_list("to_status", flat=True)),
            ["confirmed", "shipped", "delivered"],
        )

    def test_complete_premium_merchant_journey(self):
        merchant, merchant_user = self._register_verify_and_login_merchant()
        self._upgrade_to_premium(merchant, merchant_user)
        store = self._create_premium_store(merchant)
        product, variant = self._create_inventory(merchant, store)
        customer, customer_headers = self._register_verified_customer(store)
        self._complete_order(
            merchant, customer, customer_headers, store, product, variant,
        )
