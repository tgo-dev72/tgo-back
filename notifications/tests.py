"""Shared transactional email service tests (locmem backend captures mail)."""
from types import SimpleNamespace
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings

from catalog.models import CatalogSection, Product, StockVariant
from orders.models import Order
from orders.services import confirm_order, create_order
from store.models import Store, StoreConfig

from .service import (
    email_context,
    send_email,
    send_order_cancelled,
    send_order_confirmation,
    send_order_delivered,
    send_order_received,
    send_order_shipped,
)
from .platform_email import render_platform_email, send_platform_email, send_subscription_lifecycle_notice


def make_store(slug="ntf"):
    store, _ = Store.objects.get_or_create(slug=slug, defaults={"name": "Notify Store"})
    StoreConfig.objects.update_or_create(
        store=store,
        defaults={
            "from_email": "hello@notify.example.com",
            "brand_color": "#AA00BB",
            "default_locale": "en",
        },
    )
    return store


def make_order(store, email="buyer@example.com"):
    section, _ = CatalogSection.objects.get_or_create(store=store, key="soap", defaults={"label": "Soap"})
    product = Product.objects.create(store=store, section=section, ref="s-1", name="Soap", price=50)
    variant = StockVariant.objects.create(store=store, product=product, attributes={}, quantity=5)
    return create_order(
        store,
        customer={
            "name": "Buyer",
            "email": email,
            "address": "12 Test Street",
            "city": "Casablanca",
        },
        items=[{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
        payment_mode=Order.PaymentMode.ONLINE,
    )


class NotificationServiceTests(TestCase):
    def test_email_context_uses_store_config(self):
        store = make_store()
        ctx = email_context(store)
        self.assertEqual(ctx["from_email"], "hello@notify.example.com")
        self.assertEqual(ctx["brand_name"], "Notify Store")
        self.assertEqual(ctx["brand_color"], "#AA00BB")

    @override_settings(STORE_EMAIL_DOMAIN="mail.tijarago.com")
    def test_email_context_defaults_to_store_routing_alias(self):
        store = make_store(slug="teststore")
        StoreConfig.objects.filter(store=store).update(from_email="")

        ctx = email_context(store)

        self.assertEqual(ctx["from_name"], "Notify Store")
        self.assertEqual(ctx["from_email"], "teststore@mail.tijarago.com")

    def test_send_email_uses_store_identity(self):
        store = make_store()
        sent = send_email(store, subject="Hi", to="x@example.com", html="<p>hi</p>")
        self.assertTrue(sent)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["x@example.com"])
        self.assertIn("Notify Store", msg.from_email)
        self.assertIn("hello@notify.example.com", msg.from_email)

    def test_send_email_failure_returns_false(self):
        store = make_store()
        with patch("notifications.service.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp down")):
            self.assertFalse(send_email(store, subject="Hi", to="x@example.com", html="<p>hi</p>"))
        self.assertEqual(len(mail.outbox), 0)

    def test_order_confirmation_email(self):
        store = make_store()
        order = make_order(store)
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["is_paid"])
        with self.captureOnCommitCallbacks(execute=True):
            confirm_order(order)
        msg = mail.outbox[-1]
        self.assertEqual(msg.to, ["buyer@example.com"])
        self.assertEqual(msg.subject, "Your order is confirmed · Notify Store")
        self.assertNotIn(str(order.order_id), msg.subject)
        self.assertIn("50", msg.body)  # amount rendered
        self.assertEqual(msg.extra_headers.get("X-Entity-Ref-ID"), str(order.order_id))
        html = msg.alternatives[0][0]
        self.assertIn("Your selection", html)
        self.assertIn(f"#{str(order.order_id).split('-', 1)[0].upper()}", html)
        self.assertIn("font-family:Georgia,serif", html)
        self.assertIn("#AA00BB", html)
        self.assertIn('<body style="background:#ffffff;', html)
        self.assertIn('class="shell"', html)
        self.assertIn('style="background:#ffffff; border:1px solid #ded8ce;', html)

    def test_cod_receipt_is_sent_once_before_merchant_confirmation(self):
        store = make_store(slug="cod-receipt")
        section = CatalogSection.objects.create(store=store, key="receipt", label="Receipt")
        product = Product.objects.create(store=store, section=section, ref="cod-receipt", name="Soap", price=50)
        variant = StockVariant.objects.create(store=store, product=product, attributes={}, quantity=5)
        kwargs = {
            "store": store,
            "customer": {"name": "Buyer", "email": "buyer@example.com"},
            "items": [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
            "payment_mode": Order.PaymentMode.COD,
            "idempotency_key": "receipt-test-key",
            "checkout_fingerprint": "receipt-test-fingerprint",
        }
        with self.captureOnCommitCallbacks(execute=True):
            order = create_order(**kwargs)
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])
        self.assertEqual(mail.outbox[0].subject, "Your order is with us · Notify Store")
        self.assertIn("awaiting confirmation", mail.outbox[0].body)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(create_order(**kwargs).pk, order.pk)
        self.assertEqual(len(mail.outbox), 1)
        with self.captureOnCommitCallbacks(execute=True):
            confirm_order(order)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].subject, "Your order is confirmed · Notify Store")

    def test_order_lifecycle_uses_consistent_elegant_subjects(self):
        store = make_store(slug="lifecycle")
        order = make_order(store)

        send_order_received(order)
        send_order_confirmation(order)
        send_order_shipped(order)
        send_order_delivered(order)
        order.cancel_reason = '<b>Please call us</b>'
        send_order_cancelled(order)

        self.assertEqual(
            [message.subject for message in mail.outbox],
            [
                "Your order is with us · Notify Store",
                "Your order is confirmed · Notify Store",
                "Your order is on its way · Notify Store",
                "Delivered with care · Notify Store",
                "An update to your order · Notify Store",
            ],
        )
        cancelled_html = mail.outbox[-1].alternatives[0][0]
        self.assertNotIn("<b>Please call us</b>", cancelled_html)
        self.assertIn("&lt;b&gt;Please call us&lt;/b&gt;", cancelled_html)

    def test_arabic_store_receives_complete_rtl_order_email(self):
        store = make_store(slug="arabic-email")
        StoreConfig.objects.filter(store=store).update(default_locale="ar")
        order = make_order(store)

        send_order_confirmation(order)

        message = mail.outbox[-1]
        html = message.alternatives[0][0]
        self.assertEqual(message.subject, "تم تأكيد طلبك · Notify Store")
        self.assertIn("تم تأكيد اختيارك", message.body)
        self.assertIn("اختياراتك", message.body)
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("Noto Sans Arabic", html)
        self.assertIn("عنوان التوصيل", html)
        self.assertIn("الدفع قيد الانتظار", html)

    def test_no_receipt_without_customer_address(self):
        store = make_store(slug="no-receipt")
        order = make_order(store, email="")
        self.assertFalse(send_order_received(order))
        self.assertEqual(len(mail.outbox), 0)

    def test_no_email_without_customer_address(self):
        store = make_store()
        order = make_order(store, email="")
        self.assertFalse(send_order_confirmation(order))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(
    PLATFORM_NAME="TijaraGO",
    PLATFORM_TAGLINE="Commerce, made simple",
    PLATFORM_BRAND_COLOR="#1F8A8C",
    PLATFORM_BRAND_ACCENT_COLOR="#20C6C8",
    PLATFORM_BRAND_INK_COLOR="#073B3C",
    PLATFORM_BRAND_GOLD_COLOR="#D5B36A",
    PLATFORM_EMAIL_LOGO_URL="https://assets.example.test/tijarago.png",
    PLATFORM_WEBSITE_URL="https://example.test",
    PLATFORM_SUPPORT_EMAIL="support@example.test",
    DEFAULT_FROM_EMAIL="hello@example.test",
)
class PlatformEmailTests(TestCase):
    def sample(self):
        return {
            "subject": "Account ready",
            "preheader": "Your workspace is ready.",
            "eyebrow": "Welcome",
            "title": "Build your first store",
            "recipient_name": "Ada",
            "paragraphs": ["Everything is ready for you."],
            "details": [{"label": "Account", "value": "ada@example.test"}],
            "action_url": "https://example.test/start",
            "action_label": "Get started",
            "note": "You can ask for help at any time.",
        }

    def test_template_is_responsive_branded_and_has_plain_text_fallback(self):
        html, text = render_platform_email(**self.sample())
        self.assertIn("@media only screen and (max-width: 620px)", html)
        self.assertIn("#1F8A8C", html)
        self.assertIn("#20C6C8", html)
        self.assertIn("#D5B36A", html)
        self.assertIn('<body style="background:#ffffff;', html)
        self.assertIn('class="logo-wrap"', html)
        self.assertIn('style="background:#ffffff; border:1px solid #dce8e6;', html)
        self.assertNotIn("linear-gradient", html)
        self.assertIn('src="https://assets.example.test/tijarago.png"', html)
        self.assertIn("TijaraGO", html)
        self.assertIn("Build your first store", html)
        self.assertIn("ada@example.test", html)
        self.assertIn("Get started: https://example.test/start", text)

    def test_dynamic_values_are_html_escaped(self):
        content = self.sample()
        content["recipient_name"] = '<script>alert("x")</script>'
        html, _text = render_platform_email(**content)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_arabic_template_uses_rtl_safe_typography_and_spacing(self):
        html, text = render_platform_email(language="ar", **self.sample())
        self.assertIn('lang="ar" dir="rtl"', html)
        self.assertIn("font-family:&#x27;Noto Sans Arabic&#x27;,Tahoma,Arial,sans-serif", html)
        self.assertIn("line-height:58px", html)
        self.assertIn('مرحبًا <bdi dir="auto">Ada</bdi>،', html)
        self.assertIn('<bdi dir="ltr"><a href="mailto:support@example.test"', html)
        self.assertIn("unicode-bidi: isolate", html)
        self.assertIn("←", html)
        self.assertIn("مرحبًا Ada،", text)

    def test_platform_email_uses_branded_sender_reply_to_and_headers(self):
        sent = send_platform_email(
            to="merchant@example.test",
            headers={"X-TijaraGO-Event": "test"},
            **self.sample(),
        )
        self.assertTrue(sent)
        message = mail.outbox[-1]
        self.assertEqual(message.from_email, "TijaraGO <hello@example.test>")
        self.assertEqual(message.reply_to, ["support@example.test"])
        self.assertEqual(message.extra_headers["X-TijaraGO-Event"], "test")
        self.assertEqual(len(message.alternatives), 1)

    def test_platform_email_failure_is_non_fatal(self):
        with patch("notifications.platform_email.EmailMultiAlternatives.send", side_effect=RuntimeError("smtp down")):
            self.assertFalse(send_platform_email(to="merchant@example.test", **self.sample()))

    def test_subscription_expiry_notices_use_polite_non_imperative_language(self):
        expected = {
            "en": ("When you are ready", "View available packs"),
            "fr": ("Lorsque vous le souhaitez", "Voir les forfaits disponibles"),
            "ar": ("عندما يناسبك", "عرض الباقات المتاحة"),
        }
        for language, (courteous_phrase, action) in expected.items():
            with self.subTest(language=language):
                subscription = SimpleNamespace(
                    user=SimpleNamespace(
                        email=f"{language}@example.test",
                        first_name="",
                        merchant_profile=SimpleNamespace(preferred_language=language),
                    ),
                    plan=SimpleNamespace(name="Starter"),
                )
                self.assertTrue(send_subscription_lifecycle_notice(subscription, "expired"))
                message = mail.outbox[-1]
                self.assertIn(courteous_phrase, message.body)
                self.assertIn(action, message.body)
                self.assertNotIn("تدفع ثمنها", message.body)
                self.assertNotIn("Complete payment now", message.body)
                self.assertNotIn("Effectuez le paiement maintenant", message.body)

    @override_settings(TEST_MAIL="preview@example.test")
    def test_preview_command_uses_configured_test_recipient(self):
        call_command("send_test_platform_email")
        self.assertEqual(mail.outbox[-1].to, ["preview@example.test"])
        self.assertEqual(mail.outbox[-1].extra_headers["X-TijaraGO-Event"], "platform_email.preview")
