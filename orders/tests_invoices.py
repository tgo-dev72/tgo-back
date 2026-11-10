from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from PIL import Image, ImageDraw
import io

from orders.invoices import InvoiceError, _kit_snapshot, issue_invoice, render_and_store, render_invoice_pdf
from orders.invoice_template import KEYS, LABELS, NAVY, logo_flowable
from orders.views import _invoice_response
from orders.models import Invoice, Order
from orders.services import create_order
from orders.services import confirm_order, deliver_order, ship_order
from orders.tests import item, make_product, make_store
from coupons.models import Coupon
from store.models import StoreConfig, StoreMember
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan


class InvoiceTests(TestCase):
    def setUp(self):
        self.store, section = make_store(slug="invoice-store")
        self.owner = get_user_model().objects.create_user(email="owner-invoice@example.test", password="pw")
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        PlanEntitlement.objects.update_or_create(
            plan=SubscriptionPlan.objects.get(slug="free"),
            definition=EntitlementDefinition.objects.get(key="invoicing.issue"),
            defaults={"value": True},
        )
        self.product, self.variant = make_product(self.store, section, price=Decimal("120.00"), quantity=400)
        StoreConfig.objects.filter(store=self.store).update(
            currency="MAD", default_locale="fr", invoice_enabled=True, invoice_legal_name="Atelier Sable SARL",
            invoice_billing_address="18, avenue des Dunes", invoice_country="Maroc",
            invoice_contact_email="billing@example.test", invoice_identifiers={"ice": "001"},
            invoice_issuer_scope="atelier-sable", invoice_series_prefix="FAC",
            invoice_tax_regime="product", invoice_tax_rate=Decimal("20.00"), invoice_prices_include_tax=True,
        )

    def paid_order(self, ref="tx-1"):
        order = create_order(
            self.store, {"name": "Client", "email": "client@example.test"},
            [item(self.product, self.variant, 1)], Order.PaymentMode.ONLINE,
        )
        order.is_paid = Order.IsPaid.CONFIRMED
        order.transaction_id = ref
        order.save(update_fields=["is_paid", "transaction_id"])
        return order

    def logo(self, name, color, size=(240, 80)):
        output = io.BytesIO()
        Image.new("RGBA", size, color).save(output, "PNG")
        return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")

    def test_preview_pdf_renders_all_locales_without_allocating_number(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        for locale in ("fr", "en", "ar"):
            with self.subTest(locale=locale):
                StoreConfig.objects.filter(store=self.store).update(default_locale=locale)
                response = client.get(reverse("merchant-invoice-preview", args=[self.store.id]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/pdf")
                self.assertTrue(response.content.startswith(b"%PDF-"))
                self.assertIn(b"/Count 2", response.content)
        self.assertFalse(Invoice.objects.exists())

    def test_activation_allows_approved_arabic_pdf(self):
        client = APIClient(); client.force_authenticate(self.owner)
        StoreConfig.objects.filter(store=self.store).update(invoice_enabled=False, default_locale="ar")
        response = client.patch(reverse("store-config", args=[self.store.id]), {"invoice_enabled": True}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_activation_allows_partial_invoice_information_after_confirmation(self):
        client = APIClient(); client.force_authenticate(self.owner)
        StoreConfig.objects.filter(store=self.store).update(
            invoice_enabled=False,
            invoice_tax_rate=None,
            invoice_legal_name="",
            invoice_billing_address="",
            invoice_country="",
            invoice_contact_email="",
            invoice_identifiers={},
        )
        response = client.patch(
            reverse("store-config", args=[self.store.id]),
            {"invoice_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["invoice_enabled"])
        self.assertTrue(response.data["invoice_issuance_available"])

    def test_invoice_activation_preview_and_issuance_require_entitlement(self):
        PlanEntitlement.objects.filter(
            plan__slug="free",
            definition__key="invoicing.issue",
        ).update(value=False)
        StoreConfig.objects.filter(store=self.store).update(invoice_enabled=False)
        client = APIClient(); client.force_authenticate(self.owner)

        activation = client.patch(
            reverse("store-config", args=[self.store.id]),
            {"invoice_enabled": True},
            format="json",
        )
        preview = client.get(reverse("merchant-invoice-preview", args=[self.store.id]))
        StoreConfig.objects.filter(store=self.store).update(invoice_enabled=True)

        self.assertEqual(activation.status_code, 400)
        self.assertIn("invoice_enabled", activation.data)
        config = client.get(reverse("store-config", args=[self.store.id]))
        self.assertFalse(config.data["invoice_issuance_available"])
        self.assertEqual(preview.status_code, 403)
        denied_order = self.paid_order("entitlement-denied")
        self.assertIsNone(denied_order.tax_rate)
        self.assertEqual(denied_order.tax_amount, Decimal("0.00"))
        with self.assertRaisesRegex(InvoiceError, "not included"):
            issue_invoice(denied_order)

    def test_arabic_invoice_uses_embedded_unicode_font_and_renders(self):
        invoice, _ = issue_invoice(self.paid_order())
        invoice.snapshot["locale"] = "ar"
        invoice.snapshot["seller"]["legal_name"] = "أتيلييه سابل"
        invoice.snapshot["buyer"]["name"] = "رياض المثال"
        invoice.snapshot["lines"][0]["name"] = "معطر برائحة العنب"
        pdf = render_invoice_pdf(invoice.snapshot, number=invoice.number)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertIn(b"NotoSansArabic", pdf)
        self.assertIn(b"/Count 2", pdf)

    @patch("orders.invoice_template.fribidi", side_effect=RuntimeError("not installed"))
    def test_arabic_invoice_renders_with_serverless_bidi_fallback(self, _fribidi):
        invoice, _ = issue_invoice(self.paid_order("serverless-arabic"))
        invoice.snapshot["locale"] = "ar"
        invoice.snapshot["seller"]["legal_name"] = "متجر تجريبي"
        pdf = render_invoice_pdf(invoice.snapshot, number=invoice.number)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertIn(b"/Count 2", pdf)

    def test_each_store_invoice_captures_its_own_logo(self):
        self.store.logo.save("first.png", self.logo("first.png", (180, 20, 20, 255)), save=True)
        first, _ = issue_invoice(self.paid_order("first-store"))

        other, section = make_store(slug="second-logo-store")
        StoreMember.objects.create(user=self.owner, store=other, role=StoreMember.Roles.OWNER)
        product, variant = make_product(other, section, ref="second-logo-product", price=Decimal("80.00"), quantity=2)
        StoreConfig.objects.filter(store=other).update(
            invoice_enabled=True, invoice_legal_name="Second Merchant", invoice_billing_address="Rabat",
            invoice_country="Maroc", invoice_contact_email="second@example.test",
            invoice_issuer_scope="second-merchant", invoice_tax_rate=Decimal("0.00"),
        )
        other.logo.save("second.png", self.logo("second.png", (20, 40, 180, 255), (100, 100)), save=True)
        order = create_order(other, {"email": "other@example.test"}, [item(product, variant, 1)], Order.PaymentMode.ONLINE)
        order.is_paid = Order.IsPaid.CONFIRMED
        order.save(update_fields=["is_paid"])
        second, _ = issue_invoice(order)

        first_asset = first.snapshot["seller"]["logo_asset"]
        second_asset = second.snapshot["seller"]["logo_asset"]
        self.assertNotEqual(first_asset["sha256"], second_asset["sha256"])
        self.assertIn(self.store.slug, first_asset["source_name"])
        self.assertIn(other.slug, second_asset["source_name"])
        self.assertNotEqual(render_invoice_pdf(first.snapshot, number=first.number), render_invoice_pdf(second.snapshot, number=second.number))

    def test_logo_version_is_frozen_and_ready_pdf_is_never_regenerated(self):
        self.store.logo.save("old.png", self.logo("old.png", (255, 255, 255, 0)), save=True)
        old_invoice, _ = issue_invoice(self.paid_order("old-logo"))
        old_hash = old_invoice.snapshot["seller"]["logo_asset"]["sha256"]
        render_and_store(old_invoice)
        old_invoice.pdf.open("rb")
        original_pdf = old_invoice.pdf.read()
        old_invoice.pdf.close()

        self.store.logo.save("new.png", self.logo("new.png", (0, 120, 70, 255)), save=True)
        new_invoice, _ = issue_invoice(self.paid_order("new-logo"))
        self.assertNotEqual(old_hash, new_invoice.snapshot["seller"]["logo_asset"]["sha256"])

        render_and_store(old_invoice)
        old_invoice.refresh_from_db()
        old_invoice.pdf.open("rb")
        self.assertEqual(original_pdf, old_invoice.pdf.read())
        old_invoice.pdf.close()

    def test_no_storefront_logo_uses_merchant_name_and_keeps_two_page_layout(self):
        invoice, _ = issue_invoice(self.paid_order("name-fallback"))
        self.assertIsNone(invoice.snapshot["seller"]["logo_asset"])
        pdf = render_invoice_pdf(invoice.snapshot, number=invoice.number)
        self.assertIn(b"/Count 2", pdf)
        self.assertTrue(pdf.startswith(b"%PDF-"))

    def test_all_locales_render_without_payment_or_balance_due(self):
        invoice, _ = issue_invoice(self.paid_order("no-payment-pdf"))
        for locale in ("fr", "en", "ar"):
            with self.subTest(locale=locale):
                snapshot = {**invoice.snapshot, "locale": locale}
                snapshot["order"] = {
                    key: value for key, value in snapshot["order"].items()
                    if key not in {"payment_mode", "payment_status", "payment_reference"}
                }
                snapshot["totals"] = {
                    key: value for key, value in snapshot["totals"].items()
                    if key != "balance_due"
                }
                native = _kit_snapshot(
                    snapshot, number=invoice.number, issued_at=invoice.issued_at, specimen=False,
                )
                self.assertNotIn("payment", native)
                self.assertNotIn("balance_due", native["summary"])
                pdf = render_invoice_pdf(snapshot, number=invoice.number, issued_at=invoice.issued_at)
                self.assertTrue(pdf.startswith(b"%PDF-"))
                self.assertIn(b"/Count 2", pdf)

    def test_pdf_labels_exclude_payment_and_balance_due_in_every_locale(self):
        self.assertNotIn("payment", KEYS)
        self.assertNotIn("balance_due", KEYS)
        forbidden = {"Paiement", "Solde à payer", "Payment", "Balance due", "الدفع", "المبلغ المتبقي"}
        for locale, labels in LABELS.items():
            with self.subTest(locale=locale):
                self.assertTrue(forbidden.isdisjoint(labels))

    def test_temporarily_unavailable_configured_logo_aborts_then_can_retry(self):
        self.store.logo.save("retry.png", self.logo("retry.png", (30, 30, 30, 255)), save=True)
        order = self.paid_order("logo-retry")
        with patch.object(type(self.store.logo), "open", side_effect=OSError("storage unavailable")):
            with self.assertRaisesRegex(InvoiceError, "temporarily unavailable"):
                issue_invoice(order)
        self.assertFalse(Invoice.objects.filter(order=order).exists())
        invoice, created = issue_invoice(order)
        self.assertTrue(created)
        self.assertIsNotNone(invoice.snapshot["seller"]["logo_asset"])

    def test_wide_square_and_transparent_white_logos_fit_and_remain_visible(self):
        transparent = Image.new("RGBA", (400, 100), (255, 255, 255, 0))
        ImageDraw.Draw(transparent).rectangle((30, 20, 370, 80), fill=(255, 255, 255, 255))
        output = io.BytesIO()
        transparent.save(output, "PNG")
        wide, panel = logo_flowable(output.getvalue(), rtl=False)
        square, _ = logo_flowable(self.logo("square.png", (20, 90, 60, 255), (200, 200)).read(), rtl=True)
        self.assertLessEqual(wide.drawWidth, 136)
        self.assertLessEqual(wide.drawHeight, 38)
        self.assertLessEqual(square.drawWidth, 136)
        self.assertLessEqual(square.drawHeight, 38)
        self.assertEqual(panel, NAVY)

    def test_checkout_rejects_customer_fields_beyond_model_limits(self):
        client = APIClient()
        response = client.post(reverse("order-create"), {
            "payment_mode": "cod", "customer": {"name": "x" * 101},
            "items": [item(self.product, self.variant, 1)],
        }, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        self.assertEqual(response.status_code, 400)
        self.assertIn("customer", response.data)

    def test_issue_is_idempotent_and_uses_issuer_sequence(self):
        first, created = issue_invoice(self.paid_order("tx-1"))
        retried, retry_created = issue_invoice(first.order)
        second, _ = issue_invoice(self.paid_order("tx-2"))
        self.assertTrue(created)
        self.assertFalse(retry_created)
        self.assertEqual(first.pk, retried.pk)
        self.assertEqual(first.number, "FAC-2026-000001")
        self.assertEqual(second.number, "FAC-2026-000002")

    def test_issued_snapshot_does_not_change_with_settings(self):
        invoice, _ = issue_invoice(self.paid_order())
        self.store.config.invoice_legal_name = "Changed later"
        self.store.config.save(update_fields=["invoice_legal_name"])
        invoice.refresh_from_db()
        self.assertEqual(invoice.snapshot["seller"]["legal_name"], "Atelier Sable SARL")

    def test_central_tax_rate_is_added_when_prices_exclude_tax(self):
        second, second_variant = make_product(self.store, self.product.section, ref="tax-10", price=Decimal("50.00"), quantity=5)
        StoreConfig.objects.filter(store=self.store).update(invoice_prices_include_tax=False, shipping_fee=0)
        order = create_order(self.store, {"email": "tax@example.test"}, [item(self.product, self.variant, 1), item(second, second_variant, 1)], Order.PaymentMode.ONLINE)
        self.assertEqual({row["rate"] for row in order.tax_breakdown}, {"20.00"})
        self.assertEqual(order.tax_amount, Decimal("34.00"))
        self.assertEqual(order.amount, Decimal("204.00"))

    def test_zero_percentage_is_tax_exempt(self):
        StoreConfig.objects.filter(store=self.store).update(
            invoice_tax_rate=Decimal("0.00"), invoice_prices_include_tax=False, shipping_fee=0,
        )
        order = create_order(
            self.store, {"email": "exempt@example.test"},
            [item(self.product, self.variant, 1)], Order.PaymentMode.ONLINE,
        )
        self.assertEqual(order.tax_rate, Decimal("0.00"))
        self.assertEqual(order.tax_amount, Decimal("0.00"))
        self.assertEqual(order.amount, Decimal("120.00"))

    def test_invoice_uses_tax_settings_frozen_on_order(self):
        order = self.paid_order()
        StoreConfig.objects.filter(store=self.store).update(
            invoice_tax_rate=Decimal("7.00"), invoice_prices_include_tax=False,
        )
        invoice, _ = issue_invoice(order)
        self.assertEqual(invoice.snapshot["seller"]["tax_rate"], "20.00")
        self.assertTrue(invoice.snapshot["seller"]["prices_include_tax"])
        self.assertEqual(invoice.snapshot["lines"][0]["tax_rate"], "20.00")

    @patch("orders.invoices.schedule_invoice")
    def test_selected_language_is_stored_and_online_invoice_waits_for_delivery(self, schedule):
        order = self.paid_order("language-on-delivery")
        confirm_order(order, invoice_locale=Order.InvoiceLocale.ARABIC)
        order.refresh_from_db()
        self.assertEqual(order.invoice_locale, "ar")
        schedule.assert_not_called()

        ship_order(order, tracking_number="TRACK-1", carrier="Carrier")
        deliver_order(order)
        schedule.assert_called_once_with(order.pk)

        invoice, _ = issue_invoice(order)
        self.assertEqual(invoice.snapshot["locale"], "ar")

    def test_render_300_lines_contains_pdf_and_one_total_block(self):
        invoice, _ = issue_invoice(self.paid_order())
        line = invoice.snapshot["lines"][0]
        invoice.snapshot["lines"] = [{**line, "reference": f"DEMO-{index:04d}"} for index in range(1, 301)]
        pdf = render_invoice_pdf(invoice.snapshot, number=invoice.number)
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertGreater(len(pdf), 20_000)

    @override_settings(
        CLOUDINARY_STORAGE={"CLOUD_NAME": "cloud", "API_KEY": "test-key", "API_SECRET": "test-secret"},
        STORAGES={"default": {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"}},
    )
    @patch("cloudinary.uploader.upload")
    def test_cloudinary_upload_is_authenticated_and_non_pii(self, upload):
        upload.return_value = {"public_id": "private/invoices/hash", "version": 7}
        invoice, _ = issue_invoice(self.paid_order())
        render_and_store(invoice)
        kwargs = upload.call_args.kwargs
        self.assertEqual(kwargs["type"], "authenticated")
        self.assertEqual(kwargs["resource_type"], "raw")
        self.assertNotIn("client", kwargs["public_id"].lower())

    @patch("orders.views.signed_invoice_url", return_value="https://storage.example.test/invoice.pdf")
    @patch("orders.views.urlopen")
    def test_cloud_invoice_download_is_streamed_from_same_origin(self, open_url, _signed_url):
        stored = open_url.return_value.__enter__.return_value
        stored.read.return_value = b"%PDF-1.7\ncloud invoice"
        invoice, _ = issue_invoice(self.paid_order())
        invoice.status = Invoice.Status.READY
        invoice.storage_public_id = "private/invoices/hash"
        invoice.save(update_fields=["status", "storage_public_id"])

        response = _invoice_response(invoice)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response.content, b"%PDF-1.7\ncloud invoice")
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{invoice.number}.pdf"')
        open_url.assert_called_once_with("https://storage.example.test/invoice.pdf", timeout=20)

    def test_guest_download_requires_scoped_token(self):
        invoice, _ = issue_invoice(self.paid_order())
        render_and_store(invoice)
        url = reverse("guest-invoice-download", args=[self.store.slug, invoice.order.order_id])
        denied = self.client.get(url)
        self.assertEqual(denied.status_code, 404)
        wrong = signing.dumps({"order": str(invoice.order.order_id), "email": "wrong@example.test"}, salt="guest-invoice")
        self.assertEqual(self.client.get(url, {"token": wrong}).status_code, 404)
        valid = signing.dumps({"order": str(invoice.order.order_id), "email": invoice.order.customer_email}, salt="guest-invoice")
        response = self.client.get(url, {"token": valid})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        other, _ = make_store(slug="other-invoice-store")
        cross_store = reverse("guest-invoice-download", args=[other.slug, invoice.order.order_id])
        self.assertEqual(self.client.get(cross_store, {"token": valid}).status_code, 404)

    @patch("notifications.service.send_email", return_value=True)
    def test_invoice_email_reuses_pdf_as_attachment(self, send_email):
        from notifications.service import send_invoice_email
        invoice, _ = issue_invoice(self.paid_order())
        render_and_store(invoice)
        self.assertTrue(send_invoice_email(invoice))
        attachment = send_email.call_args.kwargs["attachments"][0]
        self.assertEqual(attachment[0], f"{invoice.number}.pdf")
        self.assertTrue(attachment[1].startswith(b"%PDF-"))
        self.assertEqual(attachment[2], "application/pdf")


class FreeShippingTests(TestCase):
    def setUp(self):
        self.store, section = make_store(slug="shipping-threshold")
        self.product, self.variant = make_product(self.store, section, price=Decimal("100.00"), quantity=10)
        StoreConfig.objects.filter(store=self.store).update(
            currency="USD", shipping_fee=Decimal("7.00"), free_shipping_enabled=True,
            free_shipping_threshold=Decimal("100.00"), free_shipping_currency="USD",
        )

    def test_threshold_is_strict(self):
        exact = create_order(self.store, {}, [item(self.product, self.variant, 1)], Order.PaymentMode.COD)
        self.assertEqual(exact.shipping_amount, Decimal("7.00"))
        self.product.price = Decimal("100.01")
        self.product.save(update_fields=["price"])
        self.variant.price = Decimal("100.01")
        self.variant.save(update_fields=["price"])
        above = create_order(self.store, {}, [item(self.product, self.variant, 1)], Order.PaymentMode.COD)
        self.assertEqual(above.shipping_amount, Decimal("0.00"))

    def test_coupon_is_applied_before_threshold(self):
        self.product.price = Decimal("101.00")
        self.product.save(update_fields=["price"])
        self.variant.price = Decimal("101.00")
        self.variant.save(update_fields=["price"])
        Coupon.objects.create(store=self.store, code="SAVE1", discount_type="fixed", value=Decimal("1.00"), total_usage_limit=2, per_customer_usage_limit=2)
        order = create_order(self.store, {"email": "coupon@example.test"}, [item(self.product, self.variant, 1)], Order.PaymentMode.COD, coupon_code="SAVE1")
        self.assertEqual(order.subtotal - order.discount_amount, Decimal("100.00"))
        self.assertEqual(order.shipping_amount, Decimal("7.00"))

    def test_currency_mismatch_is_rejected(self):
        StoreConfig.objects.filter(store=self.store).update(free_shipping_currency="MAD")
        with self.assertRaisesRegex(ValueError, "currency"):
            create_order(self.store, {}, [item(self.product, self.variant, 1)], Order.PaymentMode.COD)
