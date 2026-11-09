"""Immutable invoice issuance, rendering, storage and delivery."""
import hashlib
import io
import logging
import base64
from pathlib import Path
from decimal import Decimal

import arabic_reshaper
from bidi.algorithm import get_display
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus import Image as PdfImage
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PillowImage, ImageOps

from store.models import StoreConfig

from .models import Invoice, InvoiceSeries, Order
from .invoice_template import render_invoice

logger = logging.getLogger(__name__)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
PDF_FONT = "TijaraGO-DejaVu"
PDF_FONT_BOLD = "TijaraGO-DejaVu-Bold"


def _register_pdf_fonts():
    """Register repository-owned fonts so serverless hosts render identically."""
    if PDF_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PDF_FONT, FONT_DIR / "DejaVuSans.ttf"))
        pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD, FONT_DIR / "DejaVuSans-Bold.ttf"))
        pdfmetrics.registerFontFamily(PDF_FONT, normal=PDF_FONT, bold=PDF_FONT_BOLD)


def _rtl(value):
    """Shape Arabic and apply the Unicode bidi algorithm for ReportLab."""
    return get_display(arabic_reshaper.reshape(str(value or "")))


class InvoiceError(ValueError):
    pass


def _decimal(value):
    return Decimal(str(value or "0"))


def _money(value, locale="fr"):
    rendered = f"{_decimal(value):,.2f}"
    return rendered if locale == "en" else rendered.replace(",", " ").replace(".", ",")


def capture_storefront_logo(store):
    """Capture the tenant-owned storefront logo as an immutable invoice asset."""
    if not store.logo:
        return None
    source_name = str(store.logo.name or "")
    try:
        store.logo.open("rb")
        raw = store.logo.read(5 * 1024 * 1024 + 1)
        store.logo.close()
        if len(raw) > 5 * 1024 * 1024:
            raise InvoiceError("The configured storefront logo exceeds 5 MB.")
        image = PillowImage.open(io.BytesIO(raw), formats=("PNG", "JPEG", "WEBP"))
        image.verify()
        image = PillowImage.open(io.BytesIO(raw), formats=("PNG", "JPEG", "WEBP"))
        image = ImageOps.exif_transpose(image).convert("RGBA")
        if image.width * image.height > 12_000_000:
            raise InvoiceError("The configured storefront logo exceeds 12 megapixels.")
        image.thumbnail((640, 320))
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        data = output.getvalue()
        return {
            "source_name": source_name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
        }
    except InvoiceError:
        raise
    except Exception as exc:
        logger.warning("Storefront logo %s for store %s is temporarily unavailable", source_name, store.pk)
        raise InvoiceError("The configured storefront logo is temporarily unavailable; retry invoice issuance.") from exc


def _issuer_snapshot(config, order=None):
    logo_asset = capture_storefront_logo(config.store)
    snapshot = {
        "legal_name": config.invoice_legal_name[:200],
        "address": config.invoice_billing_address[:1000],
        "country": config.invoice_country[:100],
        "email": config.invoice_contact_email[:254],
        "identifiers": {key.upper(): str(value)[:80] for key, value in (config.invoice_identifiers or {}).items()},
        "tax_regime": config.invoice_tax_regime,
        "tax_rate": str(config.invoice_tax_rate) if config.invoice_tax_rate is not None else None,
        "prices_include_tax": config.invoice_prices_include_tax,
        "tax_wording": config.invoice_tax_wording[:255],
        "logo_asset": logo_asset,
    }
    if order is not None:
        snapshot["tax_rate"] = str(order.tax_rate) if order.tax_rate is not None else None
        snapshot["prices_include_tax"] = order.prices_include_tax
    return snapshot


def build_snapshot(order, config):
    lines = []
    for item in order.items.all().order_by("pk"):
        quantity = item.fulfilled_quantity or item.quantity
        lines.append({
            "name": item.name[:255], "reference": item.ref[:100], "attributes": item.attributes,
            "quantity": quantity, "unit_price": str(item.price),
            "tax_rate": str(item.tax_rate) if item.tax_rate is not None else None,
            "line_total": str((item.price * quantity).quantize(Decimal("0.01"))),
        })
    if not lines:
        raise InvoiceError("An invoice cannot be issued without order lines.")
    if order.tax_rate is None:
        raise InvoiceError("The order does not contain a tax percentage snapshot.")
    return {
        "version": 2,
        "locale": order.invoice_locale or config.default_locale,
        "seller": _issuer_snapshot(config, order),
        "buyer": {
            "name": order.customer_name[:100], "email": order.customer_email[:254],
            "phone": order.customer_phone[:20], "address": order.customer_address[:1000],
            "city": order.customer_city[:50],
        },
        "order": {
            "id": str(order.order_id), "date": order.created_at.isoformat(),
            "payment_mode": order.payment_mode, "payment_status": order.is_paid,
            "payment_reference": order.transaction_id[:100],
        },
        "currency": order.currency,
        "lines": lines,
        "totals": {
            "subtotal": str(order.subtotal), "discount": str(order.discount_amount),
            "shipping": str(order.shipping_amount), "tax": str(order.tax_amount),
            "tax_breakdown": order.tax_breakdown, "total": str(order.amount),
            "balance_due": "0.00" if order.is_paid == Order.IsPaid.CONFIRMED else str(order.amount),
        },
    }


@transaction.atomic
def issue_invoice(order):
    order = Order.objects.select_for_update().select_related("store").prefetch_related("items").get(pk=order.pk)
    existing = Invoice.objects.filter(order=order).first()
    if existing:
        return existing, False
    from subscriptions.entitlements import owner_has_store_entitlement
    from subscriptions.policies import INVOICING_KEY

    if not owner_has_store_entitlement(order.store, INVOICING_KEY):
        raise InvoiceError("Invoice issuance is not included in the store owner's current plan.")
    config = StoreConfig.objects.select_for_update().get(store=order.store)
    if not config.invoice_enabled:
        raise InvoiceError("Invoice issuance is disabled for this store.")
    if order.payment_mode == Order.PaymentMode.ONLINE and order.is_paid != Order.IsPaid.CONFIRMED:
        raise InvoiceError("Online orders can be invoiced only after payment is confirmed.")
    if order.payment_mode == Order.PaymentMode.COD and not (
        order.status == Order.Status.DELIVERED and order.is_paid == Order.IsPaid.CONFIRMED
    ):
        raise InvoiceError("Cash-on-delivery orders can be invoiced only after collection is confirmed.")
    snapshot = build_snapshot(order, config)
    year = timezone.localdate().year
    series_lookup = {"issuer_scope": config.invoice_issuer_scope, "prefix": config.invoice_series_prefix.upper(), "year": year}
    series = InvoiceSeries.objects.select_for_update().filter(**series_lookup).first()
    if series is None:
        try:
            # The savepoint keeps the outer issuance transaction usable when
            # another store sharing this legal issuer creates the row first.
            with transaction.atomic():
                series = InvoiceSeries.objects.create(**series_lookup)
        except IntegrityError:
            series = InvoiceSeries.objects.select_for_update().get(**series_lookup)
    series.last_number += 1
    series.save(update_fields=["last_number"])
    number = f"{series.prefix}-{year}-{series.last_number:06d}"
    try:
        invoice = Invoice.objects.create(
            order=order, store=order.store, series=series, number=number,
            sequence=series.last_number, snapshot=snapshot, template_version="tijarago-kit-v1",
        )
    except IntegrityError:
        invoice = Invoice.objects.get(order=order)
        return invoice, False
    return invoice, True


def _footer(canvas, doc, number, specimen, locale="fr"):
    canvas.saveState()
    canvas.setFont(PDF_FONT, 8)
    words = {
        "fr": ("Spécimen", "Facture", "APERÇU"),
        "en": ("Specimen", "Invoice", "PREVIEW"),
        "ar": ("نموذج", "فاتورة", "معاينة"),
    }.get(locale, ("Spécimen", "Facture", "APERÇU"))
    label = f"{words[0] + ' | ' if specimen else ''}{words[1]} {number or words[2]}"
    if locale == "ar":
        canvas.drawRightString(195 * mm, 10 * mm, _rtl(label))
        canvas.drawString(18 * mm, 10 * mm, str(doc.page))
    else:
        canvas.drawString(18 * mm, 10 * mm, label)
        canvas.drawRightString(195 * mm, 10 * mm, str(doc.page))
    canvas.restoreState()


def _render_invoice_pdf_legacy(snapshot, *, number="", issued_at=None, specimen=False):
    """Render every line with repeating headings and a single final totals block."""
    output = io.BytesIO()
    issued_at = issued_at or timezone.now()
    locale = snapshot.get("locale", "fr")
    _register_pdf_fonts()
    copy = {
        "fr": {"invoice": "FACTURE", "invoice_date": "Date de facture", "sale_date": "Date de vente", "order": "Commande", "seller": "Émetteur", "billed": "Facturé à", "customer": "Client", "payment": "Paiement", "paid": "Payée", "pending": "À payer", "card": "Carte bancaire", "cod": "Paiement à la livraison", "reference": "Référence", "detail": "Détail de la facture", "description": "Désignation", "qty": "Qté", "subtotal": "Sous-total articles", "discount": "Remise", "shipping": "Livraison", "tax_included": "TVA incluse", "tax": "TVA", "total": "Total TTC", "balance": "Solde à payer"},
        "en": {"invoice": "INVOICE", "invoice_date": "Invoice date", "sale_date": "Sale date", "order": "Order", "seller": "Seller", "billed": "Bill to", "customer": "Customer", "payment": "Payment", "paid": "Paid", "pending": "Amount due", "card": "Card", "cod": "Cash on delivery", "reference": "Reference", "detail": "Invoice details", "description": "Description", "qty": "Qty", "subtotal": "Merchandise subtotal", "discount": "Discount", "shipping": "Shipping", "tax_included": "Tax included", "tax": "Tax", "total": "Total", "balance": "Balance due"},
        "ar": {"invoice": "فاتورة", "invoice_date": "تاريخ الفاتورة", "sale_date": "تاريخ البيع", "order": "الطلب", "seller": "البائع", "billed": "الفاتورة إلى", "customer": "العميل", "payment": "الدفع", "paid": "مدفوعة", "pending": "مستحقة", "card": "بطاقة بنكية", "cod": "الدفع عند الاستلام", "reference": "مرجع الدفع", "detail": "تفاصيل الفاتورة", "description": "وصف المنتج", "qty": "الكمية", "subtotal": "مجموع المنتجات", "discount": "الخصم", "shipping": "الشحن", "tax_included": "الضريبة مشمولة", "tax": "ضريبة القيمة المضافة", "total": "الإجمالي شامل الضريبة", "balance": "المبلغ المتبقي"},
    }.get(locale, {})
    if not copy:
        raise InvoiceError("This invoice locale is not enabled for PDF rendering.")
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    is_rtl = locale == "ar"
    if is_rtl:
        copy = {key: _rtl(value) for key, value in copy.items()}
    small = ParagraphStyle("small", parent=styles["BodyText"], fontName=PDF_FONT, fontSize=8.5, leading=11, alignment=TA_RIGHT if is_rtl else TA_LEFT)
    right = ParagraphStyle("right", parent=small, alignment=TA_RIGHT)
    detail_heading = ParagraphStyle("detail-heading", parent=small, fontName=PDF_FONT_BOLD, fontSize=14, leading=18, alignment=TA_RIGHT if is_rtl else TA_LEFT)
    title = ParagraphStyle("invoice", parent=styles["Title"], fontName=PDF_FONT_BOLD, fontSize=20, textColor=colors.HexColor("#222222"), alignment=TA_LEFT if is_rtl else TA_RIGHT)
    seller, buyer, totals = snapshot["seller"], snapshot["buyer"], snapshot["totals"]
    order = snapshot["order"]
    seller_name = _rtl(seller["legal_name"]) if is_rtl else seller["legal_name"]
    logo = PdfImage(io.BytesIO(base64.b64decode(seller["logo_png"])), width=28 * mm, height=14 * mm, kind="proportional") if seller.get("logo_png") else Paragraph(f"<b>{seller_name}</b>", small)
    heading = [logo, Paragraph(copy["invoice"], title)]
    if is_rtl:
        heading.reverse()
    story = [
        Table([heading], colWidths=[70 * mm, 105 * mm] if is_rtl else [105 * mm, 70 * mm]),
        Spacer(1, 5 * mm),
        Table([
            [Paragraph(value, small) for value in ([copy["order"], copy["sale_date"], copy["invoice_date"]] if is_rtl else [copy["invoice_date"], copy["sale_date"], copy["order"]])],
            [Paragraph(value, small) for value in ([order["id"][:8].upper(), str(order["date"])[:10], issued_at.strftime("%d/%m/%Y")] if is_rtl else [issued_at.strftime("%d/%m/%Y"), str(order["date"])[:10], order["id"][:8].upper()])],
        ], colWidths=[58 * mm] * 3, style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), .25, colors.HexColor("#dddddd")), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)])),
        Spacer(1, 6 * mm),
    ]
    seller_lines = [seller["legal_name"], seller["address"], seller["country"]] + [f"{k} {v}" for k, v in seller.get("identifiers", {}).items()] + [seller["email"]]
    buyer_lines = [buyer.get("name") or ("العميل" if is_rtl else "Client"), buyer.get("address"), buyer.get("city"), buyer.get("email")]
    if is_rtl:
        seller_lines = [_rtl(value) for value in seller_lines]
        buyer_lines = [_rtl(value) for value in buyer_lines]
    identity_rows = [
        [Paragraph(f"<b>{copy['seller']}</b>", small), Paragraph(f"<b>{copy['billed']}</b>", small)],
        [Paragraph("<br/>".join(filter(None, seller_lines)), small), Paragraph("<br/>".join(filter(None, buyer_lines)), small)],
    ]
    if is_rtl:
        identity_rows = [list(reversed(row)) for row in identity_rows]
    story.append(Table(identity_rows, colWidths=[87 * mm, 87 * mm], style=TableStyle([("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#dddddd")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 7)])))
    paid = order.get("payment_status") == Order.IsPaid.CONFIRMED
    payment_lines = [copy["paid"] if paid else copy["pending"], copy["cod"] if order.get("payment_mode") == Order.PaymentMode.COD else copy["card"]]
    if order.get("payment_reference"):
        payment_lines.append(f"{copy['reference']} · {order['payment_reference']}")
    story.extend([Spacer(1, 7 * mm), Paragraph(f"<b>{copy['payment']}</b>", small), Spacer(1, 2 * mm), Paragraph("<br/>".join(payment_lines), small)])
    detail_story = [PageBreak(), Paragraph(copy["detail"], detail_heading), Spacer(1, 3 * mm)]
    inclusive = bool(seller.get("prices_include_tax"))
    unit_label = "Unit incl. tax" if locale == "en" and inclusive else "Unit excl. tax" if locale == "en" else _rtl("سعر الوحدة شامل الضريبة" if inclusive else "سعر الوحدة دون الضريبة") if is_rtl else "P.U. TTC" if inclusive else "P.U. HT"
    line_total_label = copy["total"] if inclusive else (_rtl("المجموع دون الضريبة") if is_rtl else "Total excl. tax" if locale == "en" else "Total HT")
    header = [Paragraph(copy["description"], small), Paragraph(copy["qty"], right), Paragraph(unit_label, right), Paragraph(copy["tax"], right), Paragraph(line_total_label, right)]
    column_widths = [82 * mm, 16 * mm, 26 * mm, 20 * mm, 31 * mm]
    if is_rtl:
        header.reverse()
        column_widths.reverse()
    line_rows = []
    for line in snapshot["lines"]:
        name = _rtl(line["name"]) if is_rtl else line["name"]
        label = f"{name}<br/><font color='#777777'>{line['reference']}</font>"
        tax = "—" if line["tax_rate"] is None else f"{line['tax_rate']} %"
        row = [Paragraph(label, small), Paragraph(str(line["quantity"]), right), Paragraph(_money(line["unit_price"], locale), right), Paragraph(tax, right), Paragraph(_money(line["line_total"], locale), right)]
        line_rows.append(list(reversed(row)) if is_rtl else row)
    detail_style = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0ee")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#333333")),
        ("LINEBELOW", (0, 0), (-1, 0), .5, colors.HexColor("#aaaaaa")), ("LINEBELOW", (0, 1), (-1, -1), .2, colors.HexColor("#e5e5e5")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ])
    detail = LongTable([header, *line_rows], repeatRows=1, colWidths=column_widths, splitByRow=True)
    detail.setStyle(detail_style)
    detail_story.append(detail)
    tax_rows = [
        [f"{copy['tax']} {row.get('rate', '0')} % · {_money(row.get('base'), locale)} {snapshot['currency']}{' · ' + copy['shipping'].lower() if row.get('shipping') else ''}", _money(row.get("tax"), locale)]
        for row in totals.get("tax_breakdown", [])
    ] or [[copy["tax_included"] if inclusive else copy["tax"], _money(totals["tax"], locale)]]
    total_rows = [
        [f"{copy['subtotal']} ({'incl.' if locale == 'en' and inclusive else 'excl.' if locale == 'en' else 'TTC' if inclusive else 'HT'})", _money(totals["subtotal"], locale)], [copy["discount"], _money(totals["discount"], locale)],
        [f"{copy['shipping']} ({'incl.' if locale == 'en' and inclusive else 'excl.' if locale == 'en' else 'TTC' if inclusive else 'HT'})", _money(totals["shipping"], locale)],
        *tax_rows,
        [copy["total"], f"{_money(totals['total'], locale)} {snapshot['currency']}"], [copy["balance"], f"{_money(totals['balance_due'], locale)} {snapshot['currency']}"],
    ]
    if is_rtl:
        total_rows = [[value, label] for label, value in total_rows]
    totals_rows = []
    for index, row in enumerate(total_rows):
        cell_style = ParagraphStyle(
            f"totals-{index}",
            parent=right if is_rtl else small,
            fontName=PDF_FONT_BOLD if index >= len(total_rows) - 2 else PDF_FONT,
        )
        totals_rows.append([Paragraph(str(cell), cell_style) for cell in row])
    totals_table = Table(totals_rows, colWidths=[45 * mm, 75 * mm] if is_rtl else [75 * mm, 45 * mm], hAlign="LEFT" if is_rtl else "RIGHT", style=TableStyle([("ALIGN", (0 if is_rtl else 1, 0), (0 if is_rtl else 1, -1), "RIGHT"), ("LINEABOVE", (0, -2), (-1, -2), .8, colors.HexColor("#333333")), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    wording = seller.get("tax_wording")
    story.extend([Spacer(1, 7 * mm), totals_table, Spacer(1, 3 * mm)])
    if wording:
        story.append(Paragraph(_rtl(wording) if is_rtl else wording, small))
    story.extend(detail_story)
    doc.build(story, onFirstPage=lambda c, d: _footer(c, d, number, specimen, locale), onLaterPages=lambda c, d: _footer(c, d, number, specimen, locale))
    return output.getvalue()


def _kit_money(value, locale):
    rendered = f"{_decimal(value):,.2f}".replace(",", " ")
    return rendered if locale == "en" else rendered.replace(".", ",")


def _kit_date(value, locale):
    if hasattr(value, "date"):
        date = value.date()
    else:
        try:
            date = timezone.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return str(value)[:10]
    if locale == "en":
        months = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
        return f"{date.day} {months[date.month - 1]} {date.year}"
    return date.strftime("%d/%m/%Y")


def _kit_snapshot(snapshot, *, number, issued_at, specimen):
    """Adapt the durable order snapshot to the supplied renderer contract."""
    locale = snapshot.get("locale", "fr")
    seller, buyer, order, totals = snapshot["seller"], snapshot["buyer"], snapshot["order"], snapshot["totals"]
    inclusive = bool(seller.get("prices_include_tax"))
    rate = _decimal(seller.get("tax_rate"))
    divisor = Decimal("100") + rate

    def net(value):
        amount = _decimal(value)
        if inclusive and rate:
            return (amount * Decimal("100") / divisor).quantize(Decimal("0.01"))
        return amount.quantize(Decimal("0.01"))

    items = []
    for line in snapshot["lines"]:
        quantity = _decimal(line["quantity"])
        unit_ht = net(line["unit_price"])
        line_ht = net(_decimal(line["unit_price"]) * quantity)
        items.append({
            "name": line["name"],
            "sku": line.get("reference", ""),
            "quantity": str(line["quantity"]),
            "unit_ht": _kit_money(unit_ht, locale),
            "tax_label": f"{_kit_money(rate, locale).removesuffix(',00').removesuffix('.00')} %" if rate else "0 %",
            "line_ht": _kit_money(line_ht, locale),
        })

    items_ht = sum((_decimal(item["line_ht"].replace(" ", "").replace(",", ".")) for item in items), Decimal("0"))
    discount_ht = net(totals["discount"])
    shipping_ht = net(totals["shipping"])
    total_ht = items_ht - discount_ht + shipping_ht
    tax_amount = _decimal(totals["tax"])
    rate_label = _kit_money(rate, locale).removesuffix(",00").removesuffix(".00")
    if locale == "fr":
        tax_label = f"TVA {rate_label} % sur {_kit_money(total_ht, locale)} {snapshot['currency']}"
    elif locale == "ar":
        tax_label = f"ضريبة القيمة المضافة \u2066({rate_label}%)\u2069"
    else:
        tax_label = f"VAT {rate_label}% on {_kit_money(total_ht, locale)} {snapshot['currency']}"

    seller_address = [line.strip() for line in str(seller.get("address") or "").splitlines() if line.strip()]
    if seller.get("country") and seller["country"] not in seller_address:
        seller_address.append(seller["country"])
    buyer_address = [line for line in (buyer.get("address"), buyer.get("city")) if line]
    identifiers = {str(key).lower(): value for key, value in seller.get("identifiers", {}).items()}
    issued_label = _kit_date(issued_at, locale)
    native = {
        "specimen": specimen,
        "locale": locale,
        "invoice_number": number or "APERÇU",
        "order_number": str(order["id"])[:8].upper(),
        "issued_on": issued_label,
        "sale_on": _kit_date(order.get("date"), locale),
        "currency": snapshot["currency"],
        "merchant": {"name": seller["legal_name"], "address": seller_address, "email": seller.get("email", ""), **identifiers},
        "customer": {"name": buyer.get("name") or ("العميل" if locale == "ar" else "Client"), "address": buyer_address},
        "items": items,
        "summary": {
            "items_ht": _kit_money(items_ht, locale),
            "discount_ht": _kit_money(-discount_ht if discount_ht else Decimal("0"), locale),
            "shipping_ht": _kit_money(shipping_ht, locale),
            "total_ht": _kit_money(total_ht, locale),
            "taxes": [{"label": tax_label, "amount": _kit_money(tax_amount, locale)}],
            "total_ttc": _kit_money(totals["total"], locale),
        },
    }
    if seller.get("tax_wording"):
        native["tax_note"] = seller["tax_wording"]
    return native


def render_invoice_pdf(snapshot, *, number="", issued_at=None, specimen=False, logo_bytes=None):
    """Render with the versioned TijaraGO kit and issue-time storefront branding."""
    issued_at = issued_at or timezone.now()
    if logo_bytes is None:
        asset = snapshot.get("seller", {}).get("logo_asset") or {}
        encoded = asset.get("data") or snapshot.get("seller", {}).get("logo_png")
        logo_bytes = base64.b64decode(encoded) if encoded else None
    native = _kit_snapshot(snapshot, number=number, issued_at=issued_at, specimen=specimen)
    return render_invoice(native, logo_bytes=logo_bytes, locale=native["locale"])


def render_and_store(invoice):
    if invoice.status == Invoice.Status.READY and (invoice.pdf or invoice.storage_public_id):
        return invoice
    invoice.status = Invoice.Status.RENDERING
    invoice.last_error = ""
    invoice.save(update_fields=["status", "last_error", "updated_at"])
    try:
        data = render_invoice_pdf(invoice.snapshot, number=invoice.number, issued_at=invoice.issued_at)
        public_id = hashlib.sha256(f"invoice:{invoice.pk}:{invoice.number}".encode()).hexdigest()
        cloudinary_backend = str(getattr(settings, "STORAGES", {}).get("default", {}).get("BACKEND", ""))
        if getattr(settings, "CLOUDINARY_STORAGE", {}).get("CLOUD_NAME") and "cloudinary" in cloudinary_backend.lower():
            import cloudinary.uploader
            uploaded = cloudinary.uploader.upload(io.BytesIO(data), resource_type="raw", type="authenticated", public_id=f"private/invoices/{public_id}", overwrite=False)
            invoice.storage_public_id = uploaded["public_id"]
            invoice.storage_version = str(uploaded.get("version", ""))
        else:
            invoice.pdf.save(f"{public_id}.pdf", ContentFile(data), save=False)
        invoice.status = Invoice.Status.READY
        invoice.save(update_fields=["pdf", "storage_public_id", "storage_version", "status", "updated_at"])
        return invoice
    except Exception as exc:
        invoice.status = Invoice.Status.ISSUED
        invoice.last_error = str(exc)[:2000]
        invoice.save(update_fields=["status", "last_error", "updated_at"])
        raise


def signed_invoice_url(invoice):
    if not invoice.storage_public_id:
        return ""
    import cloudinary.utils
    return cloudinary.utils.private_download_url(
        invoice.storage_public_id, "pdf", resource_type="raw", type="authenticated",
        attachment=f"{invoice.number}.pdf", expires_at=int(timezone.now().timestamp()) + 300,
    )


def issue_render_and_deliver(order):
    invoice, _ = issue_invoice(order)
    if invoice.status != Invoice.Status.READY:
        render_and_store(invoice)
    if invoice.delivery_status != Invoice.DeliveryStatus.SENT:
        from notifications.service import send_invoice_email
        send_invoice_email(invoice)
    return invoice


def schedule_invoice(order_id):
    def run():
        try:
            issue_render_and_deliver(Order.objects.get(pk=order_id))
        except InvoiceError:
            logger.info("Invoice was not issued for order %s because it is not eligible.", order_id)
        except Exception:
            logger.exception("Invoice job failed for order %s and can be retried safely.", order_id)
    transaction.on_commit(run)
