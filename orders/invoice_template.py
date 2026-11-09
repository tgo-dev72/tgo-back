"""Render a French, English, or Arabic TijaraGO invoice from an issued snapshot."""
import argparse
import json
from ctypes import CDLL, POINTER, byref, c_int, c_uint8, c_uint32, c_void_p
from ctypes.util import find_library
from functools import cache
from io import BytesIO
from itertools import groupby
from pathlib import Path
from unicodedata import bidirectional, category

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image as RasterImage, ImageOps
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, LongTable, PageBreak, PageTemplate,
    Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parent
LABELS = {
    "fr": (
        "Facture", "Date de facture", "Date de vente", "Commande", "Émetteur", "Facturé à",
        "Sous-total articles HT", "Remise HT", "Livraison HT", "Total HT",
        "Total TTC", "Détail de la facture", "Montants en",
        "Désignation", "Qté", "P.U. HT", "TVA", "Total HT", "Spécimen",
    ),
    "en": (
        "Invoice", "Invoice date", "Sale date", "Order", "Seller", "Bill to",
        "Items subtotal excl. tax", "Discount excl. tax", "Shipping excl. tax",
        "Total excl. tax", "Total incl. tax", "Invoice items", "Amounts in",
        "Description", "Qty", "Unit excl. tax", "Tax", "Total excl. tax", "Sample",
    ),
    "ar": (
        "فاتورة", "تاريخ الفاتورة", "تاريخ البيع", "الطلب", "البائع", "الفاتورة إلى",
        "مجموع المنتجات دون الضريبة", "الخصم دون الضريبة", "الشحن دون الضريبة",
        "الإجمالي دون الضريبة", "الإجمالي شامل الضريبة", "تفاصيل الفاتورة",
        "المبالغ بعملة", "وصف المنتج", "الكمية", "سعر الوحدة دون الضريبة",
        "الضريبة", "المجموع دون الضريبة", "نموذج",
    ),
}
KEYS = (
    "invoice_title issued_on sale_on order_number seller buyer items_ht "
    "discount_ht shipping_ht total_ht total_ttc details_title currency_label "
    "item quantity unit_ht tax_label line_ht specimen"
).split()

NAVY = colors.HexColor("#10263a")
MUTED = colors.HexColor("#465e6e")
LINE = colors.HexColor("#d6e3e7")
PALE = colors.HexColor("#f2f7f8")
MINT = colors.HexColor("#d9f2e9")
GREEN = colors.HexColor("#146855")
SEAFOAM = colors.HexColor("#9be2ca")
HERO_SUB = colors.HexColor("#d4e5ed")
WHITE = colors.white
MARGIN = 42
TOP_MARGIN = 40
ITEMS_TOP_MARGIN = 123
BOTTOM_MARGIN = 52
WIDTH = A4[0] - 2 * MARGIN
FRAME_HEIGHT = A4[1] - TOP_MARGIN - BOTTOM_MARGIN
REGULAR = "InvoiceSans"
BOLD = "InvoiceSans-Bold"
ARABIC_FONTS = {REGULAR: "InvoiceArabic", BOLD: "InvoiceArabic-Bold"}
for font, file in {
    REGULAR: "NotoSans-Regular.ttf",
    BOLD: "NotoSans-Bold.ttf",
    ARABIC_FONTS[REGULAR]: "NotoSansArabic-Regular.ttf",
    ARABIC_FONTS[BOLD]: "NotoSansArabic-Bold.ttf",
}.items():
    pdfmetrics.registerFont(TTFont(font, str(ROOT / "fonts" / file)))

LAYOUT_LABELS = {
    "fr": ("Récapitulatif", "Détail des articles à partir de la page 2", "Lignes d’articles", "Page"),
    "en": ("Invoice summary", "Item details begin on page 2", "Item lines", "Page"),
    "ar": ("ملخص الفاتورة", "تفاصيل المنتجات ابتداءً من الصفحة الثانية", "عدد بنود الفاتورة", "صفحة"),
}


def font_runs(value, font):
    """Keep Latin identifiers in Noto Sans and use Noto Sans Arabic for Arabic glyphs."""
    def font_for(char):
        is_arabic = 0x0600 <= ord(char) <= 0x08ff or 0xfb50 <= ord(char) <= 0xfefc
        return ARABIC_FONTS[font] if is_arabic else font

    return [(face, "".join(chars)) for face, chars in groupby(value, key=font_for)]


@cache
def fribidi():
    path = find_library("fribidi")
    if path is None:
        raise RuntimeError("Arabic PDF rendering requires the system FriBidi library.")
    library = CDLL(path)
    library.fribidi_get_bidi_type.argtypes = [c_uint32]
    library.fribidi_get_bidi_type.restype = c_uint32
    library.fribidi_log2vis.argtypes = [
        POINTER(c_uint32), c_int, POINTER(c_uint32), POINTER(c_uint32),
        c_void_p, c_void_p, c_void_p,
    ]
    library.fribidi_log2vis.restype = c_uint8
    return library


def visual_order(value, rtl):
    if not any(bidirectional(char) in {"R", "AL", "AN", "RLE", "RLO", "RLI"} for char in value):
        return value
    try:
        library = fribidi()
    except RuntimeError:
        # Serverless images do not always expose the native library. The
        # project's existing Unicode pipeline is a safe rendering fallback.
        # python-bidi does not accept directional-isolate controls such as
        # LRI/PDI; they are non-printing and FriBidi strips them after layout.
        sanitized = "".join(char for char in value if category(char) != "Cf")
        return get_display(arabic_reshaper.reshape(sanitized), base_dir="R" if rtl else "L")
    length = len(value)
    source = (c_uint32 * length)(*map(ord, value))
    display = (c_uint32 * length)()
    base = c_uint32(library.fribidi_get_bidi_type(ord("ا" if rtl else "A")))
    if not library.fribidi_log2vis(source, length, byref(base), display, None, None, None):
        raise ValueError("Could not shape invoice text.")
    return "".join(char for char in map(chr, display) if category(char) != "Cf")


class Text(Flowable):
    """Wrap logical text first, then shape each printed line."""
    def __init__(self, value, *, rtl=False, right=False, font=REGULAR, size=10, color=NAVY):
        super().__init__()
        self.value = str(value)
        self.rtl = rtl
        self.right = right
        self.font = font
        self.size = size
        self.color = color
        self.leading = size * (1.55 if rtl else 1.37)

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        self.lines = []
        for paragraph in self.value.split("\n"):
            line = ""
            for word in paragraph.split():
                candidate = f"{line} {word}" if line else word
                if self.fits(candidate):
                    line = candidate
                    continue
                if line:
                    self.lines.append(line)
                    line = ""
                for char in word:
                    if line and not self.fits(line + char):
                        self.lines.append(line)
                        line = ""
                    line += char
            self.lines.append(line)
        self.height = len(self.lines) * self.leading
        return self.width, self.height

    def fits(self, value):
        return self.measure(font_runs(visual_order(value, self.rtl), self.font)) <= self.width

    def measure(self, runs):
        return sum(pdfmetrics.stringWidth(part, face, self.size) for face, part in runs)

    def draw(self):
        self.canv.setFillColor(self.color)
        for index, logical in enumerate(self.lines):
            display = visual_order(logical, self.rtl)
            y = self.height - self.size - self.leading * index
            if logical != display:
                actual = (b"\xfe\xff" + logical.encode("utf-16-be")).hex()
                self.canv.addLiteral(f"/Span <</ActualText <{actual}>>> BDC")
            runs = font_runs(display, self.font)
            x = self.width - self.measure(runs) if self.right else 0
            line = self.canv.beginText(x, y)
            for face, part in runs:
                line.setFont(face, self.size)
                line.textOut(part)
            self.canv.drawText(line)
            if logical != display:
                self.canv.addLiteral("EMC")


def logo_flowable(content, rtl):
    if len(content) > 5_000_000:
        raise ValueError("Storefront logo exceeds 5 MB")
    with RasterImage.open(BytesIO(content), formats=["PNG", "JPEG", "WEBP"]) as source:
        if source.width * source.height > 12_000_000:
            raise ValueError("Storefront logo exceeds 12 megapixels")
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((640, 320))
        visible = [pixel[:3] for pixel in image.getdata() if pixel[3] >= 32]
        # White or very pale transparent artwork disappears on the kit's
        # default white badge. Keep the exact artwork and switch only the
        # badge behind it to the existing navy header colour.
        average_luminance = (
            sum((299 * red + 587 * green + 114 * blue) / 1000 for red, green, blue in visible) / len(visible)
            if visible else 255
        )
        panel = NAVY if average_luminance >= 220 else WHITE
        stream = BytesIO()
        image.save(stream, format="PNG")
    scale = min(136 / image.width, 38 / image.height)
    result = Image(stream, width=image.width * scale, height=image.height * scale)
    result.hAlign = "RIGHT" if rtl else "LEFT"
    return result, panel


def render_invoice(snapshot, *, logo_bytes=None, locale=None):
    """Return PDF bytes using finalized amounts and localized business values."""
    locale = (locale or snapshot.get("locale", "fr")).replace("_", "-").lower().split("-")[0]
    if locale not in LABELS:
        raise ValueError(f"Unsupported invoice locale {locale!r}. Choose fr, en, or ar.")
    if not snapshot["items"]:
        raise ValueError("An issued invoice must have at least one line item")
    rtl = locale == "ar"
    labels = dict(zip(KEYS, LABELS[locale], strict=True))
    invoice_no, currency = snapshot["invoice_number"], snapshot["currency"]
    seller, buyer = snapshot["merchant"], snapshot["customer"]
    summary = snapshot["summary"]

    summary_title, items_note, line_count, page_label = LAYOUT_LABELS[locale]

    def text(value, *, end=False, ltr=False, font=REGULAR, size=10, color=NAVY):
        return Text(value, rtl=rtl and not ltr, right=rtl != end, font=font,
                    size=size + (1 if rtl and not ltr else 0), color=color)

    def caption(value):
        return text(value, font=BOLD, size=8, color=MUTED)

    def table(rows, widths, *, padding=0, vertical=0, rules=(), long=False, radius=0):
        if rtl:
            rows = [row[::-1] for row in rows]
            widths = widths[::-1]
        options = {"repeatRows": 1, "splitInRow": 0} if long else {}
        if radius:
            options["cornerRadii"] = [radius] * 4
        result = (LongTable if long else Table)(rows, colWidths=widths, hAlign="LEFT", **options)
        result.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), padding),
            ("RIGHTPADDING", (0, 0), (-1, -1), padding),
            ("TOPPADDING", (0, 0), (-1, -1), vertical),
            ("BOTTOMPADDING", (0, 0), (-1, -1), vertical),
            *rules,
        ]))
        return result

    def party(title, data, identifiers):
        details = [text(title, font=BOLD, size=9, color=GREEN), Spacer(1, 6),
                   text(data["name"], font=BOLD, size=13), Spacer(1, 3)]
        details.extend(text(line, size=10) for line in data["address"])
        details.extend(text(f"{label} {data[key]}", ltr=True, size=9, color=MUTED)
                       for key, label in identifiers if data.get(key))
        if data.get("email"):
            details.append(text(data["email"], ltr=True, size=9, color=MUTED))
        return details

    brand = [text(seller["name"], font=BOLD, size=13, color=WHITE)]
    if logo_bytes:
        logo, logo_panel = logo_flowable(logo_bytes, rtl)
        badge = table([[logo]], [158], padding=10, vertical=8, radius=6,
                      rules=[("BACKGROUND", (0, 0), (0, 0), logo_panel),
                             ("ALIGN", (0, 0), (0, 0), "RIGHT" if rtl else "LEFT")])
        brand = [badge, Spacer(1, 8), *brand]
    title = [text(summary_title, end=True, font=BOLD, size=9, color=SEAFOAM),
             text(labels["invoice_title"].upper(), end=True, font=BOLD, size=34, color=WHITE),
             text(invoice_no, end=True, ltr=True, size=12, color=HERO_SUB)]
    brand_cell = (1 if rtl else 0, 0)
    header = table([[brand, title]], [WIDTH * 0.55, WIDTH * 0.45], padding=20, vertical=18, radius=12,
                   rules=[("BACKGROUND", (0, 0), (-1, 0), NAVY),
                          ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
                          ("ALIGN", brand_cell, brand_cell, "RIGHT" if rtl else "LEFT")])

    meta_keys = ("issued_on", "sale_on", "order_number")
    meta = table([[[caption(labels[key]), Spacer(1, 3), text(snapshot[key], ltr=True, font=BOLD, size=11)]
                   for key in meta_keys]], [WIDTH / 3] * 3, padding=14, vertical=10, radius=7,
                 rules=[("BACKGROUND", (0, 0), (-1, 0), PALE),
                        ("LINEAFTER", (0, 0), (1, 0), 0.55, LINE)])
    card_width = (WIDTH - 12) / 2
    parties = table([[
        party(labels["seller"], seller, (("ice", "ICE"), ("if", "IF"), ("tp", "TP"), ("rc", "RC"))),
        "", party(labels["buyer"], buyer, (("ice", "ICE"),)),
    ]], [card_width, 12, card_width], padding=14, vertical=13,
        rules=[("BOX", (0, 0), (0, 0), 0.7, LINE), ("BOX", (2, 0), (2, 0), 0.7, LINE)])

    entries = [(labels[key], summary[key]) for key in ("items_ht", "discount_ht", "shipping_ht", "total_ht")]
    entries.extend((tax["label"], tax["amount"]) for tax in summary["taxes"])
    breakdown_width = WIDTH - 284
    amounts = table([[text(label, size=9, color=MUTED), text(value, end=True, ltr=True, size=10)]
                     for label, value in entries], [breakdown_width - 82, 82], padding=12, vertical=3,
                    rules=[("LINEABOVE", (0, 3), (-1, 3), 0.55, LINE)])
    grand = table([[[text(labels["total_ttc"], font=BOLD, size=10, color=GREEN),
                     Spacer(1, 1), text(f'{summary["total_ttc"]} {currency}', ltr=True, font=BOLD, size=24)]]],
                  [268], padding=13, vertical=9, radius=8,
                  rules=[("BACKGROUND", (0, 0), (0, 0), MINT)])
    breakdown = [amounts]
    if snapshot.get("tax_note"):
        breakdown.extend([Spacer(1, 8), text(snapshot["tax_note"], size=9, color=MUTED)])
    closing = table([[breakdown, "", grand]], [breakdown_width, 16, 268])
    note = table([[text(items_note, size=9, color=MUTED),
                   text(f'{line_count} · {len(snapshot["items"])}', end=True, font=BOLD, size=9, color=GREEN)]],
                 [WIDTH * 0.65, WIDTH * 0.35], padding=11, vertical=8, radius=6,
                 rules=[("BACKGROUND", (0, 0), (-1, 0), PALE)])

    columns = ("item", "quantity", "unit_ht", "tax_label", "line_ht")
    rows = [[text(labels[key], end=index > 0, font=BOLD, size=8, color=WHITE)
             for index, key in enumerate(columns)]]
    for item in snapshot["items"]:
        name = [text(item["name"], font=BOLD, size=10)]
        if item.get("sku"):
            name.append(text(item["sku"], ltr=True, size=8, color=MUTED))
        rows.append([name, *(text(item[key], end=True, ltr=key != "tax_label",
                                 font=BOLD if key == "line_ht" else REGULAR)
                             for key in columns[1:])])
    item_table = table(rows, [WIDTH - 255, 47, 75, 51, 82], padding=10, vertical=7, long=True,
                       rules=[("BACKGROUND", (0, 0), (-1, 0), NAVY),
                              ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PALE]),
                              ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE)])

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, 42, A4[0] - MARGIN, 42)
        marker = f'{labels["specimen"]}  ·  ' if snapshot.get("specimen") else ""
        ref = text(f'{marker}{labels["invoice_title"]}  {invoice_no}', size=8, color=MUTED)
        number = text(f"{page_label} {document.page}", end=True, font=BOLD, size=8, color=GREEN)
        for element in (ref, number):
            element.wrap(WIDTH, 20)
            element.drawOn(canvas, MARGIN, 25)
        canvas.restoreState()

    def item_header(canvas, document):
        footer(canvas, document)
        canvas.saveState()
        canvas.setFillColor(GREEN)
        accent_x = A4[0] - MARGIN - 25 if rtl else MARGIN
        canvas.roundRect(accent_x, A4[1] - 48, 25, 3, 1.5, stroke=0, fill=1)
        heading = table([[
            text(labels["details_title"], font=BOLD, size=21),
            [text(invoice_no, end=True, ltr=True, font=BOLD, size=10),
             text(f'{labels["currency_label"]} {currency}', end=True, size=8, color=MUTED)],
        ]], [WIDTH * 0.61, WIDTH * 0.39])
        _, heading_height = heading.wrapOn(canvas, WIDTH, 52)
        heading.drawOn(canvas, MARGIN, A4[1] - 61 - heading_height)
        canvas.restoreState()

    cover = [header, Spacer(1, 12), meta, Spacer(1, 15), parties,
             Spacer(1, 17), closing, Spacer(1, 14), note]
    if sum(block.wrap(WIDTH, FRAME_HEIGHT)[1] for block in cover) > FRAME_HEIGHT:
        raise ValueError("Invoice summary exceeds one page. Review billing data before issuance.")

    output = BytesIO()
    doc = BaseDocTemplate(output, pagesize=A4, title=f'{labels["invoice_title"]} {invoice_no}',
                          author=seller["name"])
    for frame_id, top, on_page, next_template in (
        ("cover", TOP_MARGIN, footer, "items"), ("items", ITEMS_TOP_MARGIN, item_header, None),
    ):
        frame = Frame(MARGIN, BOTTOM_MARGIN, WIDTH, A4[1] - top - BOTTOM_MARGIN,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates(PageTemplate(id=frame_id, frames=[frame], onPage=on_page,
                                          autoNextPageTemplate=next_template))
    doc.build([*cover, PageBreak(), item_table])
    return output.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render an invoice PDF from JSON.")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--logo", type=Path)
    parser.add_argument("--locale", choices=LABELS)
    args = parser.parse_args()
    data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    args.pdf.write_bytes(render_invoice(data, locale=args.locale,
                                        logo_bytes=args.logo.read_bytes() if args.logo else None))
