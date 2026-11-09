import csv, hashlib, io, json, re, unicodedata, uuid, zipfile
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import HttpResponse
from django.utils.text import slugify
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from access_control.services import has_permission
from store.models import StoreMember
from subscriptions.policies import CATALOG_IMPORT_KEY, EntitlementDenied, enforce_operation
from .merchant_views import _merchant_store
from .models import CatalogImportOperation, CatalogSection, Product, ProductVendor, StockVariant, ensure_product_inventory_identity
from .variant_services import configure_product_options, set_variant_selections
from .rich_text import sanitize_product_description
from .remote_media import reference_product_image

MAX_FILE_BYTES = 4 * 1024 * 1024
TEMPLATE_ROWS = 1000
IMPORT_BATCH_SIZE = 25
SCHEMA, SCHEMA_VERSION = "tijarago.catalog", 1
BASIC_FIELDS = ("ref", "name", "section", "price", "description", "is_active", "quantity")
PRODUCT_FIELDS = BASIC_FIELDS + ("slug", "compare_at_price", "cost_price", "tags", "vendors", "seo_title", "seo_description", "is_featured", "track_inventory")
VARIANT_FIELDS = ("uses_variants",
                  "option1_kind", "option1_key", "option1_name", "option1_type", "option1_value", "option1_swatch",
                  "option2_kind", "option2_key", "option2_name", "option2_type", "option2_value", "option2_swatch",
                  "option3_kind", "option3_key", "option3_name", "option3_type", "option3_value", "option3_swatch",
                  "variant_sku", "variant_barcode", "variant_price", "variant_weight", "variant_quantity")
FIELDS = PRODUCT_FIELDS + VARIANT_FIELDS
REQUIRED = {"ref", "name", "section", "price"}

FIELD_HELP = (
    ("ref", "إلزامي", "مرجع فريد وثابت للمنتج. إذا كان موجوداً، يتم تحديث المنتج."),
    ("name", "إلزامي", "اسم المنتج الظاهر للزبون."),
    ("section", "إلزامي", "رمز القسم كما يظهر في ورقة «الأقسام»."),
    ("price", "إلزامي", "سعر البيع، مثل 142.08."),
    ("description", "اختياري", "وصف المنتج."),
    ("is_active", "اختياري", "true لإظهار المنتج وfalse لإخفائه."),
    ("quantity", "اختياري", "الكمية المتوفرة في المخزون."),
    ("slug", "اختياري", "رابط المنتج. اتركه فارغاً لإنشائه من الاسم."),
    ("compare_at_price", "اختياري", "السعر قبل التخفيض، ويجب أن يكون أكبر من سعر البيع."),
    ("cost_price", "اختياري", "تكلفة المنتج الداخلية."),
    ("tags", "اختياري", "وسوم مفصولة بفواصل."),
    ("vendors", "اختياري", "أسماء العلامات أو الموردين مفصولة بفواصل."),
    ("seo_title", "اختياري", "عنوان المنتج في محركات البحث."),
    ("seo_description", "اختياري", "وصف المنتج في محركات البحث."),
    ("is_featured", "اختياري", "true لإبراز المنتج وfalse لعرضه بشكل عادي."),
    ("track_inventory", "اختياري", "true لتتبع المخزون وfalse لعدم تتبعه."),
    ("uses_variants", "اختياري", "true إذا كان للمنتج مقاسات أو ألوان أو خيارات متعددة."),
    ("option1_kind", "اختياري", "اختر color أو size أو text؛ وسيضبط النظام بقية إعدادات الخيار."),
    ("option1_key", "اختياري", "معرّف ثابت لا يعتمد على اللغة، مثل size أو color أو material."),
    ("option1_name", "اختياري", "الاسم الظاهر للعميل، مثل المقاس أو اللون. كرّر ref نفسه في سطر لكل تركيبة."),
    ("option1_type", "اختياري", "نوع العرض: select للقوائم، color للألوان، أو text للنص."),
    ("option1_value", "اختياري", "قيمة الخيار الأول لهذا الصنف، مثل M أو أحمر."),
    ("option1_swatch", "اختياري", "لون بصيغة #RRGGBB عند استعمال النوع color."),
    ("option2_kind", "اختياري", "نوع الخيار الثاني: color أو size أو text."),
    ("option2_key", "اختياري", "المعرّف الثابت للخيار الثاني."),
    ("option2_name", "اختياري", "اسم الخيار الثاني عند الحاجة."),
    ("option2_type", "اختياري", "نوع عرض الخيار الثاني."),
    ("option2_value", "اختياري", "قيمة الخيار الثاني لهذا الصنف."),
    ("option2_swatch", "اختياري", "لون الخيار الثاني عند استعمال النوع color."),
    ("option3_kind", "اختياري", "نوع الخيار الثالث: color أو size أو text."),
    ("option3_key", "اختياري", "المعرّف الثابت للخيار الثالث."),
    ("option3_name", "اختياري", "اسم الخيار الثالث عند الحاجة."),
    ("option3_type", "اختياري", "نوع عرض الخيار الثالث."),
    ("option3_value", "اختياري", "قيمة الخيار الثالث لهذا الصنف."),
    ("option3_swatch", "اختياري", "لون الخيار الثالث عند استعمال النوع color."),
    ("variant_sku", "اختياري", "رمز مخزون فريد لهذا الصنف."),
    ("variant_barcode", "اختياري", "الباركود الخاص بهذا الصنف."),
    ("variant_price", "اختياري", "سعر خاص بهذا الصنف؛ اتركه فارغاً لاستعمال سعر المنتج."),
    ("variant_weight", "اختياري", "وزن هذا الصنف."),
    ("variant_quantity", "اختياري", "كمية المخزون الخاصة بهذا الصنف."),
)

BOOLEAN_FIELDS = ("is_active", "is_featured", "track_inventory", "uses_variants")
INTEGER_FIELDS = ("quantity", "variant_quantity")
DECIMAL_FIELDS = ("price", "compare_at_price", "cost_price", "variant_price", "variant_weight")
OPTION_TYPE_FIELDS = ("option1_type", "option2_type", "option3_type")
OPTION_KIND_FIELDS = ("option1_kind", "option2_kind", "option3_kind")
COLOR_PALETTE = (
    ("أسود", "Black", "Noir", "#111111"), ("أبيض", "White", "Blanc", "#ffffff"),
    ("أحمر", "Red", "Rouge", "#c62828"), ("أزرق", "Blue", "Bleu", "#1565c0"),
    ("أخضر", "Green", "Vert", "#2e7d32"), ("أصفر", "Yellow", "Jaune", "#f9a825"),
    ("برتقالي", "Orange", "Orange", "#ef6c00"), ("بنفسجي", "Purple", "Violet", "#6a1b9a"),
    ("وردي", "Pink", "Rose", "#d81b60"), ("رمادي", "Gray", "Gris", "#757575"),
    ("بني", "Brown", "Marron", "#6d4c41"), ("بيج", "Beige", "Beige", "#d7ccc8"),
    ("كحلي", "Navy", "Bleu marine", "#1a237e"), ("فيروزي", "Turquoise", "Turquoise", "#00897b"),
    ("ذهبي", "Gold", "Doré", "#c9a227"), ("فضي", "Silver", "Argenté", "#b0bec5"),
)
COLOR_ALIASES = {
    label.casefold(): swatch
    for arabic, english, french, swatch in COLOR_PALETTE
    for label in (arabic, english, french)
}
OPTION_KIND_ALIASES = {
    "color": "color", "colour": "color", "couleur": "color", "لون": "color", "اللون": "color",
    "size": "size", "taille": "size", "مقاس": "size", "المقاس": "size",
    "text": "text", "texte": "text", "نص": "text", "خيار نصي": "text",
}
OPTION_KIND_LABELS = {
    "ar": {"color": "اللون", "size": "المقاس", "text": "خيار"},
    "fr": {"color": "Couleur", "size": "Taille", "text": "Option"},
    "en": {"color": "Color", "size": "Size", "text": "Option"},
}
SIZE_VALUES = ("XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "34", "35", "36", "37", "38", "39", "40", "41", "42", "43", "44", "45", "46", "48", "50")
TEMPLATE_COPY = {
    "ar": {"product": "قميص تجريبي", "description": "احذف هذين السطرين وأضف منتجاتك", "size": "المقاس", "color": "اللون", "red": "أحمر", "products": "المنتجات", "instructions": "التعليمات", "sections": "الأقسام", "colors": "الألوان", "usage": "اختر نوع الخيار أولاً؛ ستتغير قائمة القيم حسب اللون أو المقاس أو النص."},
    "fr": {"product": "T-shirt exemple", "description": "Supprimez ces lignes et ajoutez vos produits", "size": "Taille", "color": "Couleur", "red": "Rouge", "products": "Produits", "instructions": "Instructions", "sections": "Catégories", "colors": "Couleurs", "usage": "Choisissez d’abord le type d’option ; les valeurs proposées changent pour la couleur, la taille ou le texte."},
    "en": {"product": "Sample T-shirt", "description": "Delete these rows and add your products", "size": "Size", "color": "Color", "red": "Red", "products": "Products", "instructions": "Instructions", "sections": "Sections", "colors": "Colors", "usage": "Choose the option kind first; suggested values change for color, size, or text."},
}


def _configure_excel_products_sheet(sheet, *, section_sheet=None, color_sheet=None, values_sheet=None, language="ar", simplified=False, maximum_row=TEMPLATE_ROWS + 1):
    """Apply spreadsheet guardrails; upload validation remains authoritative."""
    columns = {str(cell.value): cell.column for cell in sheet[1] if cell.value}
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(FIELDS))}{max(sheet.max_row, 2)}"
    if simplified:
        for index in range(1, 4):
            for suffix in ("key", "name", "type", "swatch"):
                column = columns.get(f"option{index}_{suffix}")
                if column: sheet.column_dimensions[get_column_letter(column)].hidden = True
    for name in ("ref", "slug", "variant_sku", "variant_barcode", "option1_key", "option2_key", "option3_key"):
        if name in columns:
            for row in range(2, max(sheet.max_row, 2) + 1): sheet.cell(row, columns[name]).number_format = "@"

    boolean = DataValidation(type="list", formula1='"true,false"', allow_blank=True)
    boolean.error = "اختر true أو false فقط."; boolean.errorTitle = "قيمة غير صالحة"; boolean.showErrorMessage = True
    sheet.add_data_validation(boolean)
    for name in BOOLEAN_FIELDS:
        if name in columns: boolean.add(f"{get_column_letter(columns[name])}2:{get_column_letter(columns[name])}{maximum_row}")

    option_type = DataValidation(type="list", formula1='"select,color,text"', allow_blank=True)
    option_type.error = "اختر select أو color أو text."; option_type.errorTitle = "نوع خيار غير صالح"; option_type.showErrorMessage = True
    sheet.add_data_validation(option_type)
    for name in OPTION_TYPE_FIELDS:
        if name in columns: option_type.add(f"{get_column_letter(columns[name])}2:{get_column_letter(columns[name])}{maximum_row}")

    kind_labels = OPTION_KIND_LABELS[language]
    kind = DataValidation(type="list", formula1='"' + ",".join((kind_labels["color"], kind_labels["size"], kind_labels["text"])) + '"', allow_blank=True)
    kind.error = "Choose color, size, or text."; kind.showErrorMessage = True
    sheet.add_data_validation(kind)
    for name in OPTION_KIND_FIELDS:
        if name in columns: kind.add(f"{get_column_letter(columns[name])}2:{get_column_letter(columns[name])}{maximum_row}")

    if values_sheet is not None:
        for index in range(1, 4):
            kind_column, value_column = columns.get(f"option{index}_kind"), columns.get(f"option{index}_value")
            if not kind_column or not value_column: continue
            kind_ref = f"{get_column_letter(kind_column)}2"
            formula = f'=IF({kind_ref}="{kind_labels["color"]}",color_values,IF({kind_ref}="{kind_labels["size"]}",size_values,text_values))'
            values = DataValidation(type="list", formula1=formula, allow_blank=True)
            values.showErrorMessage = False; values.showInputMessage = True
            values.promptTitle = "Value"; values.prompt = "Choose a suggested value or type your own."
            sheet.add_data_validation(values)
            column = get_column_letter(value_column); values.add(f"{column}2:{column}{maximum_row}")

    integer = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
    integer.error = "أدخل عدداً صحيحاً يساوي صفراً أو أكثر."; integer.errorTitle = "عدد غير صالح"; integer.showErrorMessage = True
    sheet.add_data_validation(integer)
    for name in INTEGER_FIELDS:
        if name in columns: integer.add(f"{get_column_letter(columns[name])}2:{get_column_letter(columns[name])}{maximum_row}")

    decimal = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True)
    decimal.error = "أدخل رقماً يساوي صفراً أو أكثر."; decimal.errorTitle = "رقم غير صالح"; decimal.showErrorMessage = True
    sheet.add_data_validation(decimal)
    for name in DECIMAL_FIELDS:
        if name in columns: decimal.add(f"{get_column_letter(columns[name])}2:{get_column_letter(columns[name])}{maximum_row}")

    if section_sheet is not None and "section" in columns and section_sheet.max_row >= 2:
        section = DataValidation(type="list", formula1=f"'{section_sheet.title}'!$A$2:$A${section_sheet.max_row}", allow_blank=False)
        section.error = "اختر رمز قسم موجوداً من القائمة."; section.errorTitle = "قسم غير صالح"; section.showErrorMessage = True
        sheet.add_data_validation(section)
        column = get_column_letter(columns["section"]); section.add(f"{column}2:{column}{maximum_row}")

    if color_sheet is not None and color_sheet.max_row >= 2:
        swatches = DataValidation(type="list", formula1=f"'{color_sheet.title}'!$D$2:$D${color_sheet.max_row}", allow_blank=True)
        swatches.error = "اختر لوناً من القائمة أو أدخل رمزاً بصيغة #RRGGBB."; swatches.errorTitle = "لون غير صالح"; swatches.showErrorMessage = True
        sheet.add_data_validation(swatches)
        for name in ("option1_swatch", "option2_swatch", "option3_swatch"):
            if name in columns:
                column = get_column_letter(columns[name]); swatches.add(f"{column}2:{column}{maximum_row}")


def _add_color_palette_sheet(workbook, title="الألوان"):
    sheet = workbook.create_sheet(title); sheet.append(("العربية", "English", "Français", "Hex"))
    for row in COLOR_PALETTE: sheet.append(row)
    for cell in sheet["D"][1:]:
        cell.fill = PatternFill(fill_type="solid", fgColor=str(cell.value).lstrip("#"))
    return sheet


def _add_option_values_sheet(workbook, language="ar"):
    titles = {"ar": "القيم", "fr": "Valeurs", "en": "Values"}
    sheet = workbook.create_sheet(titles[language]); sheet.append(("Colors", "Sizes", "Text"))
    color_index = {"ar": 0, "en": 1, "fr": 2}[language]
    length = max(len(COLOR_PALETTE), len(SIZE_VALUES))
    for index in range(length):
        sheet.append((COLOR_PALETTE[index][color_index] if index < len(COLOR_PALETTE) else "",
                      SIZE_VALUES[index] if index < len(SIZE_VALUES) else "", ""))
    quoted = f"'{sheet.title}'"
    workbook.defined_names.add(DefinedName("color_values", attr_text=f"{quoted}!$A$2:$A${len(COLOR_PALETTE) + 1}"))
    workbook.defined_names.add(DefinedName("size_values", attr_text=f"{quoted}!$B$2:$B${len(SIZE_VALUES) + 1}"))
    workbook.defined_names.add(DefinedName("text_values", attr_text=f"{quoted}!$C$2:$C$2"))
    return sheet


def _build_two_sheet_template(store, language):
    copy = TEMPLATE_COPY[language]; workbook = Workbook(); products = workbook.active; products.title = "Products"
    variants = workbook.create_sheet("Variants")
    products.append(TWO_SHEET_PRODUCT_FIELDS); variants.append(TWO_SHEET_VARIANT_FIELDS)
    product_rows = [
        ("TSHIRT-001", "Sample T-shirt", "general", 149, "Size and color choices", True, None, "", True, True, "sample-tshirt", 179, 80, "clothing|cotton", "Atlas", "", "", True, None, ""),
        ("SHOES-001", "Leather shoes", "general", 299, "Numeric sizes", True, None, "", True, True, "leather-shoes", 349, 170, "shoes|leather", "Atlas", "", "", True, None, ""),
        ("MUG-001", "Ceramic mug", "general", 49, "Simple product", False, 20, "", True, True, "ceramic-mug", None, 20, "home", "Atlas", "", "", False, .35, "061100000001"),
        ("NOTEBOOK-001", "Notebook", "general", 39, "Text variant choices", True, None, "", True, True, "notebook", None, 15, "stationery", "Atlas", "", "", False, .2, ""),
    ]
    variant_rows = [
        ("TSHIRT-001", "TSHIRT-M-BLK", "size_alpha", "Size", "M", "color", "Color", "Black", "", "", "", None, None, None, 10, "061100000101", .25, "", True),
        ("TSHIRT-001", "TSHIRT-M-WHT", "size_alpha", "Size", "M", "color", "Color", "White", "", "", "", 159, None, None, 5, "061100000102", .25, "", True),
        ("TSHIRT-001", "TSHIRT-XL-BLK", "size_alpha", "Size", "XL", "color", "Color", "Black", "", "", "", None, None, None, 8, "061100000103", .29, "", True),
        ("SHOES-001", "SHOE-40-BRN", "size_numeric", "Size", "40", "color", "Color", "Brown", "", "", "", None, None, None, 4, "061100000201", .8, "", True),
        ("SHOES-001", "SHOE-41-BRN", "size_numeric", "Size", "41", "color", "Color", "Brown", "", "", "", 319, None, None, 6, "061100000202", .82, "", True),
        ("SHOES-001", "SHOE-41-BLK", "size_numeric", "Size", "41", "color", "Color", "Black", "", "", "", None, None, None, 3, "061100000203", .82, "", True),
        ("NOTEBOOK-001", "NOTE-RULED", "text", "Paper", "Ruled", "", "", "", "", "", "", None, None, None, 7, "061100000301", .2, "", True),
        ("NOTEBOOK-001", "NOTE-DOTTED", "text", "Paper", "Dotted", "", "", "", "", "", "", 45, None, None, 9, "061100000302", .2, "", True),
    ]
    def parameter_row(ref, name, option_type, values):
        return (ref, name, option_type, *values, *([""] * (26 - len(values))))
    parameter_rows = [
        parameter_row("TSHIRT-001", "المقاس", "size_alpha", ["M", "XL"]),
        parameter_row("TSHIRT-001", "اللون", "color", ["أسود", "أبيض"]),
        parameter_row("SHOES-001", "المقاس", "size_numeric", ["40", "41"]),
        parameter_row("SHOES-001", "اللون", "color", ["بني", "أسود"]),
        parameter_row("NOTEBOOK-001", "نوع الورق", "text", ["مسطر", "منقط"]),
    ]
    # Downloaded templates are blank contracts. Examples live in Guide so an
    # accidental upload cannot create sample catalog records.
    products.freeze_panes = "A2"; variants.freeze_panes = "A2"
    # Instructions and reference lists deliberately live outside the contract tables.
    products["V1"] = copy["instructions"]; products["V2"] = "Products = one row per product. Variants = one row per available combination."
    products["V3"] = copy["usage"]; products["V4"] = "Changing an option name changes suggestions but does not clear an existing value."
    products["X1"] = "_dv_section"; products["Y1"] = "_dv_name"
    for row, section in enumerate(store.catalog_sections.order_by("sort_order", "key"), 2):
        products.cell(row, 24, section.key); products.cell(row, 25, section.label)
    variants["U1"] = "_dv_color"; variants["V1"] = "_dv_size_numeric"; variants["W1"] = "_dv_size_alpha"; variants["X1"] = "_dv_text"
    color_index = {"ar": 0, "en": 1, "fr": 2}[language]
    numeric_sizes = tuple(str(value) for value in range(15, 51)); alpha_sizes = ("XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL")
    text_suggestions = {"ar": ("مسطر", "منقط", "مخصص"), "fr": ("Ligné", "Pointillé", "Personnalisé"), "en": ("Ruled", "Dotted", "Custom")}[language]
    length = max(len(COLOR_PALETTE), len(numeric_sizes), len(alpha_sizes), len(text_suggestions))
    for index in range(length):
        variants.cell(index + 2, 21, COLOR_PALETTE[index][color_index] if index < len(COLOR_PALETTE) else "")
        variants.cell(index + 2, 22, numeric_sizes[index] if index < len(numeric_sizes) else "")
        variants.cell(index + 2, 23, alpha_sizes[index] if index < len(alpha_sizes) else "")
        variants.cell(index + 2, 24, text_suggestions[index] if index < len(text_suggestions) else "")
    type_validation = DataValidation(type="list", formula1='"color,size_numeric,size_alpha,text"', allow_blank=False); type_validation.showErrorMessage = True
    variants.add_data_validation(type_validation)
    for column in ("C", "F", "I"): type_validation.add(f"{column}2:{column}{TEMPLATE_ROWS + 1}")
    # Literal same-sheet ranges selected by ordinary row helper formulas.
    helper_starts = (25, 62, 99)
    for row in range(2, TEMPLATE_ROWS + 2):
        for kind_col, value_col, helper_col in (("C", "E", helper_starts[0]), ("F", "H", helper_starts[1]), ("I", "K", helper_starts[2])):
            for offset in range(36):
                source_row = offset + 2
                variants.cell(row, helper_col + offset, f'=IF(${kind_col}{row}="color",$U{source_row},IF(${kind_col}{row}="size_numeric",$V{source_row},IF(${kind_col}{row}="size_alpha",$W{source_row},$X{source_row})))')
            start, end = get_column_letter(helper_col), get_column_letter(helper_col + 35)
            validation = DataValidation(type="list", formula1=f"=${start}{row}:${end}{row}", allow_blank=True)
            validation.showErrorMessage = False; variants.add_data_validation(validation); validation.add(f"{value_col}{row}")
    for column in range(21, 136): variants.column_dimensions[get_column_letter(column)].hidden = True
    boolean = DataValidation(type="list", formula1='"true,false"', allow_blank=True); boolean.showErrorMessage = True
    products.add_data_validation(boolean)
    for column in ("F", "O", "P", "Q"): boolean.add(f"{column}2:{column}{TEMPLATE_ROWS + 1}")
    variant_boolean = DataValidation(type="list", formula1='"true,false"', allow_blank=True); variants.add_data_validation(variant_boolean); variant_boolean.add(f"S2:S{TEMPLATE_ROWS + 1}")
    for sheet, columns in ((products, ("A", "K", "T")), (variants, ("A", "B", "P"))):
        for column in columns:
            for row in range(2, sheet.max_row + 1): sheet[f"{column}{row}"].number_format = "@"
    defaults = workbook.create_sheet("Defaults")
    defaults.append(("color", "size_numeric", "size_alpha", "text", "color_hex", "kind"))
    for index in range(length):
        defaults.append((
            COLOR_PALETTE[index][color_index] if index < len(COLOR_PALETTE) else "",
            numeric_sizes[index] if index < len(numeric_sizes) else "",
            alpha_sizes[index] if index < len(alpha_sizes) else "",
            text_suggestions[index] if index < len(text_suggestions) else "",
            COLOR_PALETTE[index][3] if index < len(COLOR_PALETTE) else "",
            "",
        ))
    for index, kind_code in enumerate(("color", "size_numeric", "size_alpha", "text"), 2): defaults.cell(index, 6, kind_code)
    guide = workbook.create_sheet("Guide")
    titles = {"ar": "قالب استيراد المنتجات — TijaraGO", "fr": "Modèle d’importation des produits TijaraGO", "en": "TijaraGO product import template"}
    notes = {
        "ar": ["المنتجات: سطر واحد لكل منتج.", "Variants: سطر لكل تركيبة موجودة فعلياً.", "عند تغيير نوع الخيار، أعد اختيار القيمة لأن Excel لا يمسح القيمة القديمة تلقائياً."],
        "fr": ["Products : une ligne par produit.", "Variants : une ligne par combinaison réellement vendue.", "Après avoir changé le type d’option, resélectionnez la valeur : Excel ne supprime pas automatiquement l’ancienne valeur."],
        "en": ["Products: one row per product.", "Variants: one row per actual sellable combination.", "After changing an option kind, reselect its value; Excel does not automatically erase an earlier selection."],
    }
    guide.append((titles[language], "Version 3"))
    for note in notes[language]: guide.append((note, ""))
    output = io.BytesIO(); workbook.save(output); return output.getvalue()


GUIDE_COPY = {
    "en": {
        "title": "Add products from a file",
        "summary": "Add a simple product on one row, or repeat its reference for every size, color, or other variant.",
        "steps": [
            "Download the CSV or Excel template.",
            "Keep the column names unchanged. For products with options, repeat the ref for each combination and complete the option and variant columns.",
            "Use an existing section key from the Sections sheet. Keep the same ref when updating an existing product.",
            "Upload the file and correct any errors shown beside its rows.",
            "Review the numbers of new and updated products, then approve the import.",
        ],
    },
    "fr": {
        "title": "Ajouter des produits depuis un fichier",
        "summary": "Ajoutez un produit simple sur une seule ligne, ou répétez sa référence pour chaque taille, couleur ou autre variante.",
        "steps": [
            "Téléchargez le modèle CSV ou Excel.",
            "Conservez les noms des colonnes. Pour un produit avec options, répétez ref pour chaque combinaison et complétez les colonnes option et variante.",
            "Utilisez une clé de catégorie existante dans la feuille Sections. Conservez la même ref pour mettre à jour un produit existant.",
            "Importez le fichier et corrigez les erreurs indiquées à côté des lignes.",
            "Vérifiez le nombre de produits créés et mis à jour, puis approuvez l’import.",
        ],
    },
    "ar": {
        "title": "إضافة منتجات من ملف",
        "summary": "أضف المنتج البسيط في سطر واحد، أو كرّر مرجع المنتج في سطر لكل مقاس أو لون أو صنف.",
        "steps": [
            "نزّل نموذج CSV أو Excel.",
            "اترك أسماء الأعمدة كما هي. للمنتج ذي الخيارات، كرّر ref في سطر لكل تركيبة واملأ أعمدة option وvariant.",
            "استعمل رمز قسم موجوداً في ورقة «الأقسام»، ولا تغيّر ref عند تحديث منتج موجود.",
            "ارفع الملف وصحّح الأخطاء التي تظهر بجانب الأسطر.",
            "راجع عدد المنتجات الجديدة والمحدّثة، ثم أكّد العملية.",
        ],
    },
}


def _guide(store, language="ar"):
    language = language if language in GUIDE_COPY else "ar"
    copy = GUIDE_COPY[language]
    return {
        "title": copy["title"],
        "summary": copy["summary"],
        "steps": copy["steps"],
        "limits": {"maximum_file_bytes": MAX_FILE_BYTES, "maximum_products": None,
                   "accepted_extensions": [".csv", ".xlsx", ".json"]},
        "important_notes": [
            "يدعم CSV وExcel المنتجات البسيطة والمنتجات ذات المقاسات والألوان وحتى ثلاثة خيارات.",
            "ref موجود يحدّث المنتج، وref جديد ينشئ منتجاً جديداً.",
            "رمز قسم غير موجود أو ref مكرر يمنع إتمام العملية.",
            "إما أن تُطبّق جميع الأسطر أو لا يُطبّق أي سطر.",
        ],
        "fields": [{"name": name, "requirement": requirement, "description": description}
                   for name, requirement, description in FIELD_HELP],
        "sections": [{"key": item.key, "label": item.label}
                     for item in CatalogSection.objects.filter(store=store).order_by("sort_order", "key")],
        "templates": {
            "csv": f"/api/catalog/store/{store.pk}/import-template/?file_format=csv",
            "xlsx": f"/api/catalog/store/{store.pk}/import-template/?file_format=xlsx",
        },
    }


def _enforce_import(store):
    owner = store.members.select_related("user").get(role=StoreMember.Roles.OWNER).user
    enforce_operation(owner, "catalog.import", context={"store": store})


def _safe_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _operation_data(operation):
    meta = operation.result or {}
    return {"id": str(operation.pk), "status": operation.status, "valid": not operation.validation_errors,
            "rows": len(operation.rows), "products": len(operation.rows), "format": meta.get("format", "tabular"),
            "schema": meta.get("schema", "basic-v1"), "errors": operation.validation_errors,
            "duplicate_policy": "A matching ref updates the existing product; otherwise a product is created.",
            "commit_policy": "Resumable: each batch is atomic and progress continues from the last completed batch.", "result": meta}


def _file_url(field):
    try:
        return field.url if field else ""
    except (ValueError, AttributeError):
        return ""


def _product_document(product):
    return {
        "ref": product.ref, "name": product.name, "slug": product.slug, "description": product.description,
        "section": product.section.key, "categories": [x.key for x in product.categories.all()],
        "price": str(product.price),
        "compare_at_price": str(product.compare_at_price) if product.compare_at_price is not None else None,
        "cost_price": str(product.cost_price) if product.cost_price is not None else None,
        "tags": product.tags, "vendors": [x.name for x in product.vendors.all()],
        "seo_title": product.seo_title, "seo_description": product.seo_description,
        "attributes": product.attributes, "extra": product.extra, "variant_options": product.variant_options,
        "uses_variants": product.uses_variants, "is_active": product.is_active,
        "is_featured": product.is_featured, "track_inventory": product.track_inventory,
        "images": [{"url": _file_url(x.image), "alt_text": x.alt_text, "position": x.order}
                   for x in product.images.all() if _file_url(x.image)],
        "variants": [{"attributes": x.attributes, "price": str(x.price) if x.price is not None else None,
                      "sku": x.sku, "barcode": x.barcode, "weight": str(x.weight) if x.weight is not None else None,
                      "quantity": x.quantity, "reserved": x.reserved, "extra": x.extra}
                     for x in product.variants.all()],
    }


def _products(store):
    return Product.objects.filter(store=store).select_related("section").prefetch_related("variants", "images", "categories", "vendors")


class CatalogTransferCapabilitiesView(APIView):
    def get(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        can_export, can_import, denial = has_permission(request.user, store, "products.view"), has_permission(request.user, store, "products.create"), None
        if can_import:
            try: _enforce_import(store)
            except EntitlementDenied as exc: can_import, denial = False, exc.as_dict()
        language = request.query_params.get("lang", "ar")
        language = language if language in GUIDE_COPY else "ar"
        labels = {
            "en": ("Download products", "Download products as CSV, Excel, or JSON.", "Add products from a file", "Add or update products from a CSV, Excel, or JSON file."),
            "fr": ("Télécharger les produits", "Téléchargez les produits au format CSV, Excel ou JSON.", "Ajouter depuis un fichier", "Ajoutez ou mettez à jour des produits depuis un fichier CSV, Excel ou JSON."),
            "ar": ("تنزيل قائمة المنتجات", "تنزيل المنتجات بصيغة CSV أو Excel أو JSON.", "إضافة منتجات من ملف", "إضافة المنتجات أو تحديثها من ملف CSV أو Excel أو JSON."),
        }[language]
        return Response({"formats": ["csv", "xlsx", "json"], "guide": _guide(store, language),
            "export": {"allowed": can_export, "label": labels[0], "description": labels[1]},
            "import": {"allowed": can_import, "label": labels[2],
                       "description": labels[3],
                       "entitlement": CATALOG_IMPORT_KEY, "denial": denial}})


class CatalogImportGuideView(APIView):
    def get(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        if not has_permission(request.user, store, "products.view"):
            raise PermissionDenied("Product access is required.")
        return Response(_guide(store, request.query_params.get("lang", "ar")))


class CatalogImportTemplateView(APIView):
    def get(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        if not has_permission(request.user, store, "products.view"):
            raise PermissionDenied("Product access is required.")
        export_format = request.query_params.get("file_format", "xlsx").lower()
        language = request.query_params.get("lang", "ar").lower()
        if language not in TEMPLATE_COPY: language = "ar"
        copy = TEMPLATE_COPY[language]; kind_labels = OPTION_KIND_LABELS[language]
        if export_format == "xlsx":
            content = _build_two_sheet_template(store, language)
            response = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="products-import-template-{language}.xlsx"'
            return response
        section = store.catalog_sections.order_by("sort_order", "key").first()
        section_key = section.key if section else "your-section-key"
        if export_format == "json":
            sample = {
                "schema": SCHEMA,
                "version": SCHEMA_VERSION,
                "products": [{
                    "ref": f"{store.slug}-example-001", "name": copy["product"], "section": section_key,
                    "price": "99.00", "description": copy["description"], "is_active": False,
                    "track_inventory": True, "uses_variants": True,
                    "variant_options": [{"key": "size", "label": copy["size"], "type": "select", "values": ["M", "L"]}],
                    "variants": [
                        {"sku": f"{store.slug}-EXAMPLE-M", "attributes": {"size": "M"}, "quantity": 5, "price": "99.00"},
                        {"sku": f"{store.slug}-EXAMPLE-L", "attributes": {"size": "L"}, "quantity": 3, "price": "105.00"},
                    ],
                }],
            }
            response = HttpResponse(json.dumps(sample, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="products-import-sample-{language}.json"'
            return response
        base = ("EXAMPLE-001", copy["product"], section_key, "99.00", copy["description"],
                "true", "", "example-product", "129.00", "60.00", "تجريبي,جديد", "علامة تجريبية",
                copy["product"], copy["description"], "false", "true", "20.00")
        examples = [
            base + ("true", kind_labels["size"], "size", copy["size"], "select", "M", "", kind_labels["color"], "color", copy["color"], "color", copy["red"], "#c62828") + ("",) * 6 + ("EXAMPLE-M-RED", "", "99.00", "0.250", 5),
            base + ("true", kind_labels["size"], "size", copy["size"], "select", "L", "", kind_labels["color"], "color", copy["color"], "color", copy["red"], "#c62828") + ("",) * 6 + ("EXAMPLE-L-RED", "", "105.00", "0.270", 3),
        ]
        if export_format == "csv":
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = 'attachment; filename="products-import-template.csv"'
            response.write("\ufeff"); writer = csv.writer(response, lineterminator="\r\n")
            writer.writerow(FIELDS); writer.writerows(examples)
            return response
        return Response({"format": "Choose csv or xlsx."}, status=400)


class CatalogExportView(APIView):
    def get(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        if not has_permission(request.user, store, "products.view"): raise PermissionDenied("Product access is required.")
        products, export_format = list(_products(store)), request.query_params.get("file_format", "csv").lower()
        if export_format == "json":
            body = json.dumps({"schema": SCHEMA, "version": SCHEMA_VERSION,
                               "products": [_product_document(x) for x in products]}, ensure_ascii=False, indent=2)
            response = HttpResponse(body, content_type="application/json; charset=utf-8")
            response["Content-Disposition"] = f'attachment; filename="{store.slug}-products.json"'
            return response
        rows = []
        for p in products:
            variants = list(p.variants.all())
            base = (p.ref, p.name, p.section.key, p.price, p.description, str(p.is_active).lower(),
                    sum(x.quantity for x in variants) if not p.uses_variants else "", p.slug,
                    p.compare_at_price, p.cost_price, ",".join(p.tags),
                    ",".join(x.name for x in p.vendors.all()), p.seo_title, p.seo_description,
                    str(p.is_featured).lower(), str(p.track_inventory).lower())
            option_defs = list(p.variant_options or [])[:3]
            if p.uses_variants and variants:
                for variant in variants:
                    option_cells = []
                    for index in range(3):
                        definition = option_defs[index] if index < len(option_defs) else {}
                        key = str(definition.get("key") or "")
                        value = variant.attributes.get(key, "") if key else ""
                        swatch = ""
                        for candidate in definition.get("values", []):
                            if isinstance(candidate, dict) and str(candidate.get("value") or candidate.get("label") or "") == str(value):
                                swatch = candidate.get("swatch") or ""; break
                        kind = "color" if definition.get("type") == "color" else "size" if key == "size" else "text"
                        option_cells.extend((kind, key, definition.get("label") or key, definition.get("type") or "select", value, swatch))
                    values = base + ("true", *option_cells, variant.sku, variant.barcode,
                                     variant.price, variant.weight, variant.quantity)
                    rows.append([_safe_cell(x) for x in values])
            else:
                values = base + ("false",) + ("",) * 18 + ("", "", "", "", "")
                rows.append([_safe_cell(x) for x in values])
        if export_format == "xlsx":
            workbook, output = Workbook(), io.BytesIO(); sheet = workbook.active; sheet.title = "Products"; sheet.append(FIELDS)
            for row in rows: sheet.append(row)
            sections = workbook.create_sheet("Sections"); sections.append(("section", "name"))
            for item in store.catalog_sections.order_by("sort_order", "key"): sections.append((item.key, item.label))
            colors = _add_color_palette_sheet(workbook, "Colors")
            _configure_excel_products_sheet(sheet, section_sheet=sections, color_sheet=colors)
            workbook.save(output); response = HttpResponse(output.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="{store.slug}-products.xlsx"'
        elif export_format == "csv":
            response = HttpResponse(content_type="text/csv; charset=utf-8"); response.write("\ufeff")
            response["Content-Disposition"] = f'attachment; filename="{store.slug}-products.csv"'
            writer = csv.writer(response, lineterminator="\r\n"); writer.writerow(FIELDS); writer.writerows(rows)
        else: return Response({"format": "Choose csv, xlsx, or json."}, status=400)
        response["X-Export-Fields"] = ",".join(FIELDS); return response


def _decimal(value, errors, field, nullable=False, places="0.01"):
    if value in (None, "") and nullable: return None
    try:
        number = Decimal(str(value))
        if number < 0: raise InvalidOperation
        return str(number.quantize(Decimal(places)))
    except (InvalidOperation, ValueError, TypeError):
        errors[field] = "Enter a non-negative decimal amount."; return None if nullable else "0.00"


def _boolean(value, errors, field, default):
    if value in (None, ""): return default
    if isinstance(value, bool): return value
    value = str(value).strip().lower()
    if value not in {"true", "false", "1", "0"}: errors[field] = "Use true or false."
    return value in {"true", "1"}


def _list(value):
    if value in (None, ""): return []
    if isinstance(value, list): return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value).split(",") if x.strip()]


def _external_available(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _from_external_catalog(source, sections, default_section="", policy=None):
    policy = policy or {}
    source_options = source.get("options") or []
    source_variants = source.get("variants") or []
    simple_default = (
        len(source_options) == 1
        and str(source_options[0].get("name") or "").strip().casefold() == "title"
        and [str(item).strip().casefold() for item in source_options[0].get("values") or []] == ["default title"]
        and len(source_variants) == 1
        and str(source_variants[0].get("option1") or "").strip().casefold() == "default title"
        and source_variants[0].get("option2") in (None, "")
        and source_variants[0].get("option3") in (None, "")
    )
    option_keys, options = [], []
    for position, option in enumerate([] if simple_default else source_options, 1):
        label = str(option.get("name") or "Option").strip(); key = slugify(label).replace("-", "_")[:50] or "option"
        if key in option_keys:
            key = f"{key}_{position}"
        kind = OPTION_KIND_ALIASES.get(label.casefold(), "text")
        option_type = "color" if kind == "color" else "select" if kind in {"size_numeric", "size_alpha"} else "text"
        option_keys.append(key); options.append({"key": key, "label": label, "kind": kind, "type": option_type, "values": option.get("values") or []})
    variants = []
    for position, item in enumerate(source_variants, 1):
        available = _external_available(item.get("available"))
        if not available and not policy.get("include_unavailable", True):
            continue
        attrs = {key: str(item.get(f"option{i}") or "").strip() for i, key in enumerate(option_keys, 1)
                 if item.get(f"option{i}") not in (None, "Default Title")}
        grams = item.get("grams")
        weight = str((Decimal(str(grams)) / Decimal("1000")).quantize(Decimal("0.001"))) if grams not in (None, "", 0, "0") else None
        external_id = str(item.get("id") or position)
        source_quantity = item.get("inventory_quantity")
        try:
            quantity = max(0, int(source_quantity)) if source_quantity not in (None, "") else int(
                policy.get("default_available_stock", 0) if available
                else policy.get("default_unavailable_stock", 0)
            )
        except (TypeError, ValueError):
            quantity = 0
        source_compare = item.get("compare_at_price")
        try:
            valid_compare = source_compare if source_compare not in (None, "") and Decimal(str(source_compare)) > Decimal(str(item.get("price") or 0)) else None
        except InvalidOperation:
            valid_compare = None
        variants.append({
            "attributes": attrs, "price": item.get("price"), "compare_at_price": valid_compare,
            "cost_price": None, "sku": str(item.get("sku") or f"IMP-{source.get('id')}-{external_id}"),
            "barcode": str(item.get("barcode") or ""), "weight": weight, "quantity": quantity, "reserved": 0,
            "is_active": True, "source_system": "external_public_json", "source_id": external_id,
            "extra": {}, "source_metadata": {"source_available": item.get("available"), "source_created_at": item.get("created_at"),
                      "source_updated_at": item.get("updated_at"),
                      "stock_state": "source_inventory_quantity" if source_quantity not in (None, "") else "unknown_initialized_zero",
                      "weight_state": "unknown" if weight is None else "source_grams",
                      "source_compare_at_price_rejected": source_compare if source_compare not in (None, "") and valid_compare is None else None},
            "source_featured_image": item.get("featured_image"),
        })
    if variants and not any(x["attributes"] for x in variants):
        options, option_keys = [], []
    hint = slugify(str(source.get("product_type") or ""))[:50]
    section = hint if hint in sections else default_section
    images = [{"source_id": str(item.get("id") or ""), "url": str(item.get("src") or "").strip(),
               "position": item.get("position") or 0, "alt_text": str(item.get("alt") or ""),
               "variant_ids": [str(value) for value in item.get("variant_ids") or []]}
              for item in source.get("images") or [] if item.get("src")]
    image_by_id = {item["source_id"]: item for item in images}
    associated = {}
    for image in images:
        for variant_id in image["variant_ids"]:
            associated.setdefault(variant_id, []).append(image["source_id"])
    for variant in variants:
        featured = variant.pop("source_featured_image", None)
        featured_id = str(featured.get("id") or "") if isinstance(featured, dict) else ""
        candidates = associated.get(variant["source_id"], [])
        chosen = featured_id or (candidates[0] if len(candidates) == 1 else "")
        variant["source_image_id"] = chosen
        variant["image_url"] = str((image_by_id.get(chosen) or {}).get("url") or (featured or {}).get("src") or "") if chosen or isinstance(featured, dict) else ""
        variant["image_conflict"] = bool(featured_id and candidates and featured_id not in candidates) or len(set(candidates)) > 1
    first_variant = variants[0] if variants else {}
    reference_prefix = slugify(str(policy.get("reference_prefix") or "catalog"))[:50] or "catalog"
    return {"ref": f"{reference_prefix}-{source.get('id')}", "name": source.get("title"), "slug": source.get("handle"),
            "description": sanitize_product_description(source.get("body_html") or ""), "section": section,
            "price": next((x.get("price") for x in source_variants if x.get("price") not in (None, "")), "0"),
            "compare_at_price": first_variant.get("compare_at_price"),
            "tags": source.get("tags"), "vendors": [source.get("vendor")] if source.get("vendor") else [],
            "variant_options": options, "uses_variants": bool(options and variants), "variants": variants,
            "is_active": bool(policy.get("publish_products", False)), "track_inventory": True,
            "source_system": "external_public_json", "source_id": str(source.get("id") or ""),
            "images": images, "extra": {}, "source_metadata": {"source_created_at": source.get("created_at"), "source_updated_at": source.get("updated_at"),
                                             "source_published_at": source.get("published_at"), "source_stock_policy": "merchant_import_policy"}}


def _normalize(source, sections, number, external_catalog=False, default_section="", external_policy=None):
    if external_catalog: source = _from_external_catalog(source, sections, default_section, external_policy)
    errors = {}; row = {"ref": str(source.get("ref") or "").strip(), "name": str(source.get("name") or "").strip(),
        "slug": str(source.get("slug") or "").strip(), "description": str(source.get("description") or ""),
        "section": str(source.get("section") or "").strip(), "source_row": number}
    for field in ("ref", "name", "section"):
        if not row[field]: errors[field] = "Required."
    if row["section"] and row["section"] not in sections: errors["section"] = "Unknown section key for this store."
    row.update({"section_id": sections.get(row["section"]), "price": _decimal(source.get("price"), errors, "price"),
        "compare_at_price": _decimal(source.get("compare_at_price"), errors, "compare_at_price", True),
        "cost_price": _decimal(source.get("cost_price"), errors, "cost_price", True),
        "is_active": _boolean(source.get("is_active"), errors, "is_active", True),
        "is_featured": _boolean(source.get("is_featured"), errors, "is_featured", False),
        "track_inventory": _boolean(source.get("track_inventory"), errors, "track_inventory", True),
        "uses_variants": _boolean(source.get("uses_variants"), errors, "uses_variants", False),
        "tags": _list(source.get("tags")), "vendors": _list(source.get("vendors")),
        "seo_title": str(source.get("seo_title") or ""), "seo_description": str(source.get("seo_description") or ""),
        "attributes": source.get("attributes") if isinstance(source.get("attributes"), dict) else {},
        "extra": source.get("extra") if isinstance(source.get("extra"), dict) else {},
        "variant_options": source.get("variant_options") if isinstance(source.get("variant_options"), list) else [],
        "categories": _list(source.get("categories"))})
    row["source_system"] = str(source.get("source_system") or "")
    row["source_id"] = str(source.get("source_id") or "")
    row["source_metadata"] = source.get("source_metadata") if isinstance(source.get("source_metadata"), dict) else {}
    row["images"] = source.get("images") if isinstance(source.get("images"), list) else []
    unknown_categories = sorted(set(row["categories"]) - set(sections))
    if unknown_categories: errors["categories"] = f"Unknown section keys: {', '.join(unknown_categories)}"
    if row["compare_at_price"] is not None and Decimal(row["compare_at_price"]) <= Decimal(row["price"]):
        errors["compare_at_price"] = "Compare-at price must be greater than the selling price."
    variants = source.get("variants") if isinstance(source.get("variants"), list) else []
    if not variants: variants = [{"attributes": {}, "quantity": source.get("quantity") or 0}]
    row["variants"] = []
    for i, variant in enumerate(variants, 1):
        if not isinstance(variant, dict): errors[f"variants.{i}"] = "Must be an object."; continue
        try:
            quantity, reserved = int(variant.get("quantity") or 0), int(variant.get("reserved") or 0)
            if quantity < 0 or reserved < 0 or reserved > quantity: raise ValueError
        except (TypeError, ValueError): errors[f"variants.{i}.quantity"] = "Quantity must be non-negative and at least reserved stock."; quantity = reserved = 0
        variant_errors = {}
        row["variants"].append({"attributes": variant.get("attributes") if isinstance(variant.get("attributes"), dict) else {},
            "price": _decimal(variant.get("price"), variant_errors, "price", True), "sku": str(variant.get("sku") or ""),
            "barcode": str(variant.get("barcode") or ""),
            "weight": _decimal(variant.get("weight"), variant_errors, "weight", True, "0.001"),
            "compare_at_price": _decimal(variant.get("compare_at_price"), variant_errors, "compare_at_price", True),
            "cost_price": _decimal(variant.get("cost_price"), variant_errors, "cost_price", True),
            "quantity": quantity, "reserved": reserved, "is_active": bool(variant.get("is_active", True)),
            "source_system": str(variant.get("source_system") or ""), "source_id": str(variant.get("source_id") or ""),
            "source_metadata": variant.get("source_metadata") if isinstance(variant.get("source_metadata"), dict) else {},
            "source_image_id": str(variant.get("source_image_id") or ""), "image_url": str(variant.get("image_url") or ""),
            "extra": variant.get("extra") if isinstance(variant.get("extra"), dict) else {}})
        for key, value in variant_errors.items(): errors[f"variants.{i}.{key}"] = value
        if variant.get("image_conflict"):
            errors[f"variants.{i}.image"] = "Conflicting explicit source image associations; make featured_image and images[].variant_ids agree."
    return row, ({"row": number, "ref": row["ref"], "fields": errors} if errors else None)


def _parse_json(raw, sections, default_section="", external_policy=None):
    try: payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError): return [], [{"row": 1, "fields": {"file": "The JSON document is invalid or is not UTF-8."}}], "json"
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
        return [], [{"row": 1, "fields": {"file": "JSON must contain a products array."}}], "json"
    native = payload.get("schema") == SCHEMA
    if payload.get("schema") and not native: return [], [{"row": 1, "fields": {"schema": "Unsupported catalog schema."}}], "json"
    if native and payload.get("version") != SCHEMA_VERSION: return [], [{"row": 1, "fields": {"version": "Unsupported schema version."}}], SCHEMA
    rows, errors, seen = [], [], set()
    for number, source in enumerate(payload["products"], 1):
        if not isinstance(source, dict): errors.append({"row": number, "fields": {"product": "Must be an object."}}); continue
        if not native and not (external_policy or {}).get("include_unavailable", True):
            variants = source.get("variants") if isinstance(source.get("variants"), list) else []
            if variants and not any(_external_available(item.get("available")) for item in variants if isinstance(item, dict)):
                continue
        row, error = _normalize(source, sections, number, not native, default_section, external_policy)
        if row["ref"] in seen:
            error = error or {"row": number, "ref": row["ref"], "fields": {}}; error["fields"]["ref"] = "Duplicate reference in this file."
        seen.add(row["ref"]); rows.append(row)
        if error: errors.append(error)
    return rows, errors, SCHEMA if native else "external-products-json-v1"


TWO_SHEET_SCHEMA = "native-two-sheet-v3"
TWO_SHEET_PRODUCT_FIELDS = (
    "ref", "name", "section", "price", "description", "uses_variants", "quantity", "image_urls",
    "is_active", "track_inventory", "slug", "compare_at_price", "cost_price", "tags", "vendors",
    "seo_title", "seo_description", "is_featured", "weight", "barcode",
)
TWO_SHEET_VARIANT_FIELDS = (
    "product_ref", "sku",
    "option1_kind", "option1_name", "option1_value",
    "option2_kind", "option2_name", "option2_value",
    "option3_kind", "option3_name", "option3_value",
    "price", "compare_at_price", "cost_price", "quantity", "barcode", "weight", "image_url", "is_active",
)
PARAMETER_FIELDS = ("product_ref", "option_name", "option_type") + tuple(f"value_{index}" for index in range(1, 27))
PARAMETER_TYPES = {"color", "size_numeric", "size_alpha", "text"}


def _normalized_label(value):
    return unicodedata.normalize("NFC", str(value or "").strip()).casefold()


def _sheet_error(sheet, row, column, code, message, suggestion=""):
    return {"sheet": sheet, "row": row, "column": column, "code": code,
            "message": message, "suggestion": suggestion, "fields": {column: message}}


def _workbook_records(sheet, expected, table_name):
    errors, records = [], []
    table = sheet.tables.get(table_name)
    if table is not None:
        from openpyxl.utils.cell import range_boundaries
        min_col, min_row, max_col, max_row = range_boundaries(table.ref)
    else:
        min_col, min_row, max_col, max_row = 1, 1, sheet.max_column, sheet.max_row
    header_cells = list(sheet.iter_rows(min_row=min_row, max_row=min_row, min_col=min_col, max_col=max_col))[0]
    headers = [str(cell.value or "").strip() for cell in header_cells]
    positions = {}
    for index, header in enumerate(headers):
        if not header: continue
        if header in positions:
            errors.append(_sheet_error(sheet.title, min_row, header, "duplicate_header", f"Duplicate header: {header}."))
        positions[header] = index
    for header in expected:
        if header not in positions:
            errors.append(_sheet_error(sheet.title, min_row, header, "missing_header", f"Missing required contract column: {header}."))
    if errors: return records, errors
    for excel_row in range(min_row + 1, max_row + 1):
        cells = list(sheet.iter_rows(min_row=excel_row, max_row=excel_row, min_col=min_col, max_col=max_col))[0]
        values = {}
        formula = False
        for header in expected:
            cell = cells[positions[header]]
            if cell.data_type == "f":
                errors.append(_sheet_error(sheet.title, excel_row, header, "formula_unsupported",
                                           "Formula cells are not supported in imported fields.", "Paste the calculated value."))
                formula = True
            values[header] = cell.value
        if formula: continue
        if all(value in (None, "") or (isinstance(value, str) and not value.strip()) for value in values.values()): continue
        records.append((excel_row, values))
    return records, errors


def _required_boolean(value, errors, sheet, row, column, *, default=None):
    if value in (None, ""):
        if default is None: errors.append(_sheet_error(sheet, row, column, "required", "This value is required."))
        return default
    local = {}; parsed = _boolean(value, local, column, False)
    if local: errors.append(_sheet_error(sheet, row, column, "invalid_boolean", "Use true or false (or 1 or 0)."))
    return parsed


def _pipe_list(value):
    if value in (None, ""): return []
    return list(dict.fromkeys(part.strip() for part in str(value).split("|") if part.strip()))


def _valid_remote_url(value):
    try:
        URLValidator(schemes=["http", "https"])(value)
        return True
    except DjangoValidationError:
        return False


def _option_identity(label, position):
    text = str(label or "").strip(); folded = text.casefold()
    kind = OPTION_KIND_ALIASES.get(folded)
    if kind == "size": return "size", "select"
    if kind == "color": return "color", "color"
    key = slugify(text).replace("-", "_")[:50] or f"option_{position}"
    return key, "text"


def _parse_two_sheet_workbook(workbook, sections, store):
    errors = []
    if not {"Products", "Variants"}.issubset(workbook.sheetnames):
        return [], [_sheet_error("Workbook", 1, "sheets", "missing_sheet", "The workbook requires Products and Variants sheets.")], TWO_SHEET_SCHEMA
    products, product_errors = _workbook_records(workbook["Products"], TWO_SHEET_PRODUCT_FIELDS, "ProductsData")
    variants, variant_errors = _workbook_records(workbook["Variants"], TWO_SHEET_VARIANT_FIELDS, "VariantsData")
    errors.extend(product_errors); errors.extend(variant_errors)
    if errors: return [], errors, TWO_SHEET_SCHEMA
    by_ref, product_locations = {}, {}
    existing_refs = set(Product.objects.filter(store=store).values_list("ref", flat=True))
    existing_slugs = set(Product.objects.filter(store=store).exclude(slug="").values_list("slug", flat=True))
    existing_skus = set(StockVariant.objects.filter(store=store).exclude(sku="").values_list("sku", flat=True))
    existing_barcodes = set(StockVariant.objects.filter(store=store).exclude(barcode="").values_list("barcode", flat=True))
    for number, source in products:
        clean = {key: value.strip() if isinstance(value, str) else value for key, value in source.items()}
        ref = str(clean.get("ref") or "").strip(); name = str(clean.get("name") or "").strip(); section = str(clean.get("section") or "").strip()
        if not ref: errors.append(_sheet_error("Products", number, "ref", "required", "Product reference is required.")); continue
        if ref in by_ref: errors.append(_sheet_error("Products", number, "ref", "duplicate_ref", "Product reference is duplicated in this workbook.")); continue
        if ref in existing_refs: errors.append(_sheet_error("Products", number, "ref", "existing_ref", "This product reference already exists. Two-sheet imports are create-only."))
        if not name: errors.append(_sheet_error("Products", number, "name", "required", "Product name is required."))
        if section not in sections: errors.append(_sheet_error("Products", number, "section", "unknown_section", "Choose a section belonging to this store."))
        local = {}; price = _decimal(clean.get("price"), local, "price")
        if local: errors.append(_sheet_error("Products", number, "price", "invalid_decimal", local["price"]))
        uses_variants = _required_boolean(clean.get("uses_variants"), errors, "Products", number, "uses_variants")
        is_active = _required_boolean(clean.get("is_active"), errors, "Products", number, "is_active", default=True)
        featured = _required_boolean(clean.get("is_featured"), errors, "Products", number, "is_featured", default=False)
        tracked = _required_boolean(clean.get("track_inventory"), errors, "Products", number, "track_inventory", default=True)
        slug = str(clean.get("slug") or "").strip()
        if slug and slug in existing_slugs: errors.append(_sheet_error("Products", number, "slug", "slug_conflict", "This product URL is already used in the store."))
        compare_errors = {}; compare = _decimal(clean.get("compare_at_price"), compare_errors, "compare_at_price", True)
        cost = _decimal(clean.get("cost_price"), compare_errors, "cost_price", True)
        weight = _decimal(clean.get("weight"), compare_errors, "weight", True, "0.001")
        for field, message in compare_errors.items(): errors.append(_sheet_error("Products", number, field, "invalid_decimal", message))
        if compare is not None and Decimal(compare) <= Decimal(price): errors.append(_sheet_error("Products", number, "compare_at_price", "invalid_discount", "Compare-at price must be greater than selling price."))
        quantity = clean.get("quantity")
        try: quantity = None if quantity in (None, "") else int(quantity)
        except (TypeError, ValueError): quantity = None; errors.append(_sheet_error("Products", number, "quantity", "invalid_integer", "Quantity must be a nonnegative integer."))
        if quantity is not None and quantity < 0: errors.append(_sheet_error("Products", number, "quantity", "invalid_integer", "Quantity must be a nonnegative integer."))
        if uses_variants and (quantity is not None or str(clean.get("barcode") or "").strip()): errors.append(_sheet_error("Products", number, "quantity", "variant_stock_conflict", "Variant products cannot have product-level quantity or barcode."))
        if not uses_variants and tracked and quantity is None: errors.append(_sheet_error("Products", number, "quantity", "required", "Quantity is required when inventory tracking is enabled."))
        row = {"ref": ref, "name": name, "section": section, "section_id": sections.get(section), "price": price,
               "description": str(clean.get("description") or ""), "slug": slug, "compare_at_price": compare, "cost_price": cost,
               "tags": _pipe_list(clean.get("tags")), "vendors": _pipe_list(clean.get("vendors")), "seo_title": str(clean.get("seo_title") or ""),
               "seo_description": str(clean.get("seo_description") or ""), "is_active": is_active, "is_featured": featured,
               "track_inventory": tracked, "uses_variants": uses_variants, "attributes": {}, "extra": {}, "categories": [],
               "variant_options": [], "variants": [], "image_urls": _pipe_list(clean.get("image_urls")), "source_row": number}
        for image_url in row["image_urls"]:
            if not _valid_remote_url(image_url):
                errors.append(_sheet_error("Products", number, "image_urls", "invalid_image_url", "Use direct public HTTP or HTTPS image URLs separated by |."))
        if not uses_variants:
            if ref in existing_skus: errors.append(_sheet_error("Products", number, "ref", "sku_conflict", "The generated stock SKU conflicts with an existing store SKU."))
            product_barcode = str(clean.get("barcode") or "")
            if product_barcode and product_barcode in existing_barcodes: errors.append(_sheet_error("Products", number, "barcode", "barcode_conflict", "Barcode must be unique within the store."))
            row["variants"] = [{"attributes": {}, "price": None, "sku": ref, "barcode": str(clean.get("barcode") or ""),
                                "weight": weight, "quantity": quantity or 0, "reserved": 0, "extra": {}}]
        by_ref[ref] = row; product_locations[ref] = number
    # Native rows define product-owned options directly. Build the same
    # normalized definition input formerly supplied by the Parameters sheet.
    synthesized = {}
    for number, source in variants:
        ref = str(source.get("product_ref") or "").strip()
        for index in range(1, 4):
            kind = str(source.get(f"option{index}_kind") or "").strip()
            label = unicodedata.normalize("NFC", str(source.get(f"option{index}_name") or "").strip())
            value = unicodedata.normalize("NFC", str(source.get(f"option{index}_value") or "").strip())
            present = (kind, label, value)
            if any(present) and not all(present):
                errors.append(_sheet_error("Variants", number, f"option{index}_kind", "incomplete_option", "Kind, name and value must all be filled."))
                continue
            if not kind:
                continue
            if kind not in PARAMETER_TYPES:
                errors.append(_sheet_error("Variants", number, f"option{index}_kind", "unsupported_option_type", "Use color, size_numeric, size_alpha, or text."))
                continue
            identity = (ref, _normalized_label(label))
            definition = synthesized.setdefault(identity, {"product_ref": ref, "option_name": label, "option_type": kind, "values": []})
            if definition["option_type"] != kind:
                errors.append(_sheet_error("Variants", number, f"option{index}_kind", "inconsistent_option_kind", "The same product option must use one kind on every row."))
            if value.casefold() not in {item.casefold() for item in definition["values"]}:
                definition["values"].append(value)
    parameters = []
    for offset, definition in enumerate(synthesized.values(), 2):
        values = definition.pop("values")
        parameters.append((offset, {**definition, **{f"value_{index}": value for index, value in enumerate(values, 1)}}))
    parameter_map, parameter_keys = {}, {}
    for number, source in parameters:
        ref = str(source.get("product_ref") or "").strip(); label = unicodedata.normalize("NFC", str(source.get("option_name") or "").strip())
        option_type = str(source.get("option_type") or "").strip().lower()
        if not ref: errors.append(_sheet_error("Parameters", number, "product_ref", "required", "Product reference is required.")); continue
        product = by_ref.get(ref)
        if product is None: errors.append(_sheet_error("Parameters", number, "product_ref", "orphan_parameter", "No Products row matches this reference.")); continue
        if not product["uses_variants"]: errors.append(_sheet_error("Parameters", number, "product_ref", "simple_product_parameter", "Simple products cannot define variant options."))
        if not label: errors.append(_sheet_error("Parameters", number, "option_name", "required", "Option name is required.")); continue
        if option_type not in PARAMETER_TYPES:
            errors.append(_sheet_error("Parameters", number, "option_type", "unsupported_option_type", "Use color, size_numeric, size_alpha, or text.")); continue
        normalized = _normalized_label(label); product_options = parameter_map.setdefault(ref, {})
        if normalized in product_options:
            errors.append(_sheet_error("Parameters", number, "option_name", "duplicate_parameter", "This product option is defined more than once.")); continue
        raw_values = [unicodedata.normalize("NFC", str(source.get(f"value_{index}") or "").strip()) for index in range(1, 27)]
        allowed, seen = [], set()
        for value in raw_values:
            if not value or value.casefold() in seen: continue
            seen.add(value.casefold()); allowed.append(value)
        if not allowed:
            errors.append(_sheet_error("Parameters", number, "value_1", "missing_values", "Add at least one allowed value.")); continue
        familiar = OPTION_KIND_ALIASES.get(label.casefold())
        key = (
            "color" if option_type == "color" and familiar == "color" else
            "size" if option_type in {"size_numeric", "size_alpha"} and familiar == "size" else
            slugify(label).replace("-", "_")[:50] or f"option_{len(product_options) + 1}"
        )
        used_keys = parameter_keys.setdefault(ref, set())
        if key in used_keys:
            errors.append(_sheet_error("Parameters", number, "option_name", "option_alias_collision", "This option conflicts with another option for the product.")); continue
        used_keys.add(key)
        display_type = "color" if option_type == "color" else "text" if option_type == "text" else "select"
        configured_values = []
        for value in allowed:
            swatch = COLOR_ALIASES.get(value.casefold()) if option_type == "color" else None
            configured_values.append({"value": value, "label": value, "swatch": swatch} if swatch else value)
        definition = {"key": key, "label": label, "kind": option_type, "type": display_type, "values": configured_values,
                      "allowed": {value.casefold(): value for value in allowed}, "parameter_type": option_type}
        product_options[normalized] = definition; product["variant_options"].append({key: value for key, value in definition.items() if key not in {"allowed", "parameter_type"}})
    seen_skus, seen_barcodes, seen_combinations, schemas = set(), set(), {}, {}
    for number, source in variants:
        clean = {key: value.strip() if isinstance(value, str) else value for key, value in source.items()}
        ref = str(clean.get("product_ref") or "").strip(); product = by_ref.get(ref)
        if not ref: errors.append(_sheet_error("Variants", number, "product_ref", "required", "Product reference is required.")); continue
        if product is None: errors.append(_sheet_error("Variants", number, "product_ref", "orphan_variant", "No Products row matches this reference.")); continue
        if not product["uses_variants"]: errors.append(_sheet_error("Variants", number, "product_ref", "simple_product_variant", "A simple product cannot have Variants rows.")); continue
        pairs = []
        for index in range(1, 4):
            name, value = str(clean.get(f"option{index}_name") or "").strip(), str(clean.get(f"option{index}_value") or "").strip()
            if bool(name) != bool(value): errors.append(_sheet_error("Variants", number, f"option{index}_value", "incomplete_option", "Option name and value must both be filled."))
            if name and value: pairs.append((name, value))
            elif index < 3 and any(str(clean.get(f"option{later}_name") or "").strip() for later in range(index + 1, 4)):
                errors.append(_sheet_error("Variants", number, f"option{index}_name", "option_gap", "Option pairs cannot contain gaps."))
        if not pairs: errors.append(_sheet_error("Variants", number, "option1_name", "required", "At least one option pair is required.")); continue
        keys, definitions, attributes = [], [], {}
        for index, (label, value) in enumerate(pairs, 1):
            definition = parameter_map.get(ref, {}).get(_normalized_label(label))
            if definition is None:
                errors.append(_sheet_error("Variants", number, f"option{index}_name", "unknown_parameter", "Define this option for the product in Parameters.")); continue
            canonical = definition["allowed"].get(value.casefold())
            if canonical is None:
                errors.append(_sheet_error("Variants", number, f"option{index}_value", "value_not_allowed", "Choose a value allowed by this product's Parameters row.")); continue
            key = definition["key"]
            if key in keys: errors.append(_sheet_error("Variants", number, f"option{index}_name", "duplicate_option", "Option names resolve to the same product option."))
            keys.append(key); definitions.append(definition); attributes[key] = canonical
        schema = tuple(keys)
        if ref in schemas and schemas[ref] != schema: errors.append(_sheet_error("Variants", number, "option1_name", "inconsistent_schema", "Every variant of a product must use the same option names in the same positions."))
        schemas.setdefault(ref, schema)
        signature = tuple(attributes[key].casefold() for key in keys)
        if signature in seen_combinations.setdefault(ref, set()): errors.append(_sheet_error("Variants", number, "option1_value", "duplicate_combination", "This option combination is duplicated."))
        seen_combinations[ref].add(signature)
        sku = str(clean.get("sku") or "").strip()
        if not sku: errors.append(_sheet_error("Variants", number, "sku", "required", "Variant SKU is required."))
        elif sku in seen_skus or sku in existing_skus: errors.append(_sheet_error("Variants", number, "sku", "sku_conflict", "SKU must be unique within the store."))
        seen_skus.add(sku)
        barcode = str(clean.get("barcode") or "").strip()
        if barcode and (barcode in existing_barcodes or barcode in seen_barcodes): errors.append(_sheet_error("Variants", number, "barcode", "barcode_conflict", "Barcode must be unique within the store."))
        if barcode: seen_barcodes.add(barcode)
        quantity = clean.get("quantity")
        try: quantity = None if quantity in (None, "") else int(quantity)
        except (TypeError, ValueError): quantity = None; errors.append(_sheet_error("Variants", number, "quantity", "invalid_integer", "Quantity must be a nonnegative integer."))
        if quantity is not None and quantity < 0: errors.append(_sheet_error("Variants", number, "quantity", "invalid_integer", "Quantity must be a nonnegative integer."))
        if product["track_inventory"] and quantity is None: errors.append(_sheet_error("Variants", number, "quantity", "required", "Variant quantity is required when inventory tracking is enabled."))
        local = {}; variant_price = _decimal(clean.get("price"), local, "price", True); variant_weight = _decimal(clean.get("weight"), local, "weight", True, "0.001")
        variant_compare = _decimal(clean.get("compare_at_price"), local, "compare_at_price", True)
        variant_cost = _decimal(clean.get("cost_price"), local, "cost_price", True)
        for field, message in local.items(): errors.append(_sheet_error("Variants", number, field, "invalid_decimal", message))
        effective_price = Decimal(variant_price if variant_price is not None else product["price"])
        if variant_compare is not None and Decimal(variant_compare) <= effective_price:
            errors.append(_sheet_error("Variants", number, "compare_at_price", "invalid_discount", "Compare-at price must be greater than the effective selling price."))
        variant_active = _required_boolean(clean.get("is_active"), errors, "Variants", number, "is_active", default=True)
        variant_image_url = str(clean.get("image_url") or "").strip()
        if variant_image_url and variant_image_url not in product["image_urls"]:
            errors.append(_sheet_error("Variants", number, "image_url", "image_not_in_gallery", "Variant image URL must also appear in this product's image_urls gallery."))
        product["variants"].append({"attributes": attributes, "price": variant_price,
                                    "compare_at_price": variant_compare, "cost_price": variant_cost,
                                    "sku": sku, "barcode": barcode, "weight": variant_weight, "is_active": variant_active,
                                    "quantity": quantity or 0, "reserved": 0, "extra": {}, "image_url": variant_image_url, "source_row": number})
    for ref, product in by_ref.items():
        if product["uses_variants"] and not parameter_map.get(ref):
            errors.append(_sheet_error("Products", product_locations[ref], "uses_variants", "missing_options", "Variant products require at least one complete option on a Variants row."))
        if product["uses_variants"] and not product["variants"]:
            errors.append(_sheet_error("Products", product_locations[ref], "uses_variants", "missing_variants", "Variant products require at least one Variants row."))
    return list(by_ref.values()), errors, TWO_SHEET_SCHEMA


def _csv_records(raw, filename, expected):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [_sheet_error(filename, 1, "file", "invalid_encoding", "CSV files must use UTF-8 encoding.")]
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
        headers = [str(value or "").strip() for value in reader.fieldnames or []]
        missing = [field for field in expected if field not in headers]
        if missing:
            return [], [_sheet_error(filename, 1, "header", "missing_header", f"Missing columns: {', '.join(missing)}.")]
        records = []
        for number, source in enumerate(reader, 2):
            business = {field: source.get(field) for field in expected}
            if all(value in (None, "") or not str(value).strip() for value in business.values()):
                continue
            records.append((number, business))
        return records, []
    except csv.Error:
        return [], [_sheet_error(filename, 1, "file", "malformed_csv", "The CSV quoting or row structure is invalid.")]


def _parse_native_csv_pair(products_raw, variants_raw, sections, store):
    products, errors = _csv_records(products_raw, "Products.csv", TWO_SHEET_PRODUCT_FIELDS)
    variants = []
    if variants_raw is not None:
        variants, variant_errors = _csv_records(variants_raw, "Variants.csv", TWO_SHEET_VARIANT_FIELDS)
        errors.extend(variant_errors)
    if errors:
        return [], errors, TWO_SHEET_SCHEMA
    workbook = Workbook()
    product_sheet = workbook.active; product_sheet.title = "Products"
    variant_sheet = workbook.create_sheet("Variants")
    product_sheet.append(TWO_SHEET_PRODUCT_FIELDS); variant_sheet.append(TWO_SHEET_VARIANT_FIELDS)
    for _, row in products: product_sheet.append([row[field] for field in TWO_SHEET_PRODUCT_FIELDS])
    for _, row in variants: variant_sheet.append([row[field] for field in TWO_SHEET_VARIANT_FIELDS])
    rows, parse_errors, schema = _parse_two_sheet_workbook(workbook, sections, store)
    if variants_raw is None and any(row.get("uses_variants") for row in rows):
        parse_errors.append(_sheet_error("Products.csv", 1, "uses_variants", "missing_variants_file", "Upload Variants.csv together with Products.csv when any product uses variants."))
    return rows, parse_errors, schema


def _has_native_product_headers(raw):
    try:
        text = raw.decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;")
        headers = next(csv.reader(io.StringIO(text), dialect), [])
        return set(TWO_SHEET_PRODUCT_FIELDS).issubset({str(item).strip() for item in headers})
    except (UnicodeDecodeError, csv.Error):
        return False


def _has_native_variant_headers(raw):
    try:
        text = raw.decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;")
        headers = next(csv.reader(io.StringIO(text), dialect), [])
        return set(TWO_SHEET_VARIANT_FIELDS).issubset({str(item).strip() for item in headers})
    except (UnicodeDecodeError, csv.Error):
        return False


def _tabular_variant_source(source):
    """Translate merchant-friendly option columns into the native variant document."""
    source = dict(source)
    options, attributes, errors = [], {}, {}
    for index in range(1, 4):
        kind_text = str(source.get(f"option{index}_kind") or "").strip()
        kind = OPTION_KIND_ALIASES.get(kind_text.casefold()) if kind_text else None
        if kind_text and kind is None:
            errors[f"option{index}_kind"] = "Choose color, size, or text."
        language = "ar" if re.search(r"[\u0600-\u06ff]", kind_text) else "fr" if kind_text.casefold() in {"couleur", "taille", "texte"} else "en"
        raw_key = str(source.get(f"option{index}_key") or "").strip()
        name = str(source.get(f"option{index}_name") or "").strip()
        option_type = str(source.get(f"option{index}_type") or "select").strip().lower()
        value = str(source.get(f"option{index}_value") or "").strip()
        swatch = str(source.get(f"option{index}_swatch") or "").strip().lower()
        if kind:
            raw_key = raw_key or (kind if kind != "text" else f"option_{index}")
            name = name or OPTION_KIND_LABELS[language][kind]
            option_type = "color" if kind == "color" else "text" if kind == "text" else "select"
        if bool(name) != bool(value):
            errors[f"option{index}"] = "Option name and value must be provided together."
            continue
        if not name:
            continue
        key = slugify(raw_key or name).replace("-", "_")[:50] or f"option_{index}"
        if option_type not in {"select", "color", "text"}:
            errors[f"option{index}_type"] = "Use select, color, or text."
            option_type = "select"
        if option_type == "color" and not swatch:
            swatch = COLOR_ALIASES.get(value.casefold(), "")
        if option_type == "color" and not re.fullmatch(r"#[0-9a-f]{6}", swatch):
            errors[f"option{index}_swatch"] = "Choose a known color or enter a six-digit value such as #c62828."
        if key in attributes:
            errors[f"option{index}_name"] = "Option names on the same row must be different."
            continue
        option_value = {"value": value, "label": value, "swatch": swatch} if option_type == "color" else value
        options.append({"key": key, "label": name, "type": option_type, "values": [option_value]})
        attributes[key] = value
    variant_columns = any(source.get(field) not in (None, "") for field in VARIANT_FIELDS[1:])
    requested = str(source.get("uses_variants") or "").strip().lower() in {"true", "1"}
    if requested or variant_columns:
        source["uses_variants"] = True
        source["variant_options"] = options
        source["variants"] = [{
            "attributes": attributes,
            "sku": source.get("variant_sku") or "",
            "barcode": source.get("variant_barcode") or "",
            "price": source.get("variant_price") or None,
            "weight": source.get("variant_weight") or None,
            "quantity": source.get("variant_quantity") if source.get("variant_quantity") not in (None, "") else source.get("quantity") or 0,
            "reserved": 0,
            "extra": {},
        }]
    return source, errors


def _merge_tabular_variant_row(target, incoming, number, errors):
    fields = {}
    for field in ("name", "section", "price"):
        if target[field] != incoming[field]:
            fields[field] = "Repeated product rows must use the same product details."
    existing_skus = {item["sku"] for item in target["variants"] if item["sku"]}
    existing_attributes = {json.dumps(item["attributes"], sort_keys=True, ensure_ascii=False) for item in target["variants"]}
    for variant in incoming["variants"]:
        signature = json.dumps(variant["attributes"], sort_keys=True, ensure_ascii=False)
        if signature in existing_attributes:
            fields["variants"] = "Each variant option combination must be unique."
        if variant["sku"] and variant["sku"] in existing_skus:
            fields["variant_sku"] = "Variant SKU must be unique within the product."
        existing_attributes.add(signature); existing_skus.add(variant["sku"])
        target["variants"].append(variant)
    definitions = {item["key"]: item for item in target["variant_options"]}
    for definition in incoming["variant_options"]:
        current = definitions.get(definition["key"])
        if current is None:
            target["variant_options"].append(definition); definitions[definition["key"]] = definition
        else:
            if current["label"] != definition["label"] or current["type"] != definition["type"]:
                fields["variant_options"] = "Repeated option keys must use the same name and type."
            for value in definition["values"]:
                identity = str(value.get("value") or value.get("label") or "") if isinstance(value, dict) else str(value)
                existing = {
                    str(item.get("value") or item.get("label") or "") if isinstance(item, dict) else str(item)
                    for item in current["values"]
                }
                if identity not in existing: current["values"].append(value)
    if fields: errors.append({"row": number, "ref": incoming["ref"], "fields": fields})


def _parse_tabular(raw, filename, sections, store=None):
    try:
        if filename.endswith(".xlsx"):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 20 * 1024 * 1024: return [], [{"row": 1, "fields": {"file": "Expanded Excel workbook is too large."}}], "basic-v1"
            workbook = load_workbook(io.BytesIO(raw), read_only=False, data_only=False, keep_links=False)
            if {"Products", "Variants"}.issubset(workbook.sheetnames):
                if store is None: return [], [_sheet_error("Workbook", 1, "store", "required", "Store context is required.")], TWO_SHEET_SCHEMA
                return _parse_two_sheet_workbook(workbook, sections, store)
            values = workbook.active.iter_rows(values_only=True)
            fields = [str(x or "").strip() for x in next(values, ())]
            reader = (dict(zip(fields, ("" if x is None else str(x) for x in row))) for row in values)
        else:
            csv_reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")); fields, reader = csv_reader.fieldnames or [], csv_reader
    except (UnicodeDecodeError, ValueError, KeyError, zipfile.BadZipFile): return [], [{"row": 1, "fields": {"file": "The CSV or Excel file is invalid."}}], "basic-v1"
    rows, errors, by_ref = [], [], {}; missing = sorted(REQUIRED - set(fields))
    if missing: errors.append({"row": 1, "fields": {"header": f"Missing columns: {', '.join(missing)}"}})
    try:
        for number, source in enumerate(reader, 2):
            source = {k: v.strip() if isinstance(v, str) else v for k, v in source.items() if k}
            source, tabular_errors = _tabular_variant_source(source)
            row, error = _normalize(source, sections, number)
            if tabular_errors:
                error = error or {"row": number, "ref": row["ref"], "fields": {}}
                error["fields"].update(tabular_errors)
            previous = by_ref.get(row["ref"])
            if previous is not None:
                if previous["uses_variants"] and row["uses_variants"]:
                    _merge_tabular_variant_row(previous, row, number, errors)
                else:
                    error = error or {"row": number, "ref": row["ref"], "fields": {}}
                    error["fields"]["ref"] = "Duplicate reference in this file. Use variant columns for repeated product rows."
            else:
                by_ref[row["ref"]] = row; rows.append(row)
            if error: errors.append(error)
    except csv.Error: errors.append({"row": 1, "fields": {"file": "Malformed CSV."}})
    schema = "tabular-variants-v2" if set(fields) & set(VARIANT_FIELDS) else "basic-v1"
    return rows, errors, schema


class CatalogImportValidateView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    def post(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        if not has_permission(request.user, store, "products.create"): raise PermissionDenied("Product creation access is required.")
        try: _enforce_import(store)
        except EntitlementDenied as exc: return Response(exc.as_dict(), status=status.HTTP_403_FORBIDDEN)
        uploads = request.FILES.getlist("files")
        upload = request.FILES.get("file") or request.FILES.get("products_file") or (uploads[0] if uploads else None)
        variants_upload = request.FILES.get("variants_file") or (uploads[1] if len(uploads) > 1 else None)
        if upload is None: return Response({"file": "Choose a CSV, Excel (.xlsx), or JSON file."}, status=400)
        if upload.size > MAX_FILE_BYTES or (variants_upload and variants_upload.size > MAX_FILE_BYTES): return Response({"file": f"Files are limited to {MAX_FILE_BYTES} bytes each."}, status=400)
        filename = upload.name.lower()
        if not filename.endswith((".csv", ".xlsx", ".json")): return Response({"file": "Choose a CSV, Excel (.xlsx), or JSON file."}, status=400)
        raw = upload.read(MAX_FILE_BYTES + 1)
        variants_raw = variants_upload.read(MAX_FILE_BYTES + 1) if variants_upload else None
        if variants_raw is not None and _has_native_variant_headers(raw) and _has_native_product_headers(variants_raw):
            upload, variants_upload = variants_upload, upload
            raw, variants_raw = variants_raw, raw
            filename = upload.name.lower()
        external_policy = None
        try:
            section_resolutions = json.loads(request.data.get("section_resolutions") or "{}")
            if not isinstance(section_resolutions, dict): raise ValueError("section_resolutions")
        except (TypeError, ValueError, json.JSONDecodeError):
            return Response({"section_resolutions": "Provide valid section choices."}, status=400)
        if filename.endswith(".json"):
            def policy_bool(name, default):
                value = request.data.get(name)
                if value in (None, ""): return default
                normalized = str(value).strip().lower()
                if normalized not in {"true", "false", "1", "0"}:
                    raise ValueError(name)
                return normalized in {"true", "1"}

            def policy_stock(name):
                value = int(str(request.data.get(name, "0")).strip() or "0")
                if not 0 <= value <= 1_000_000: raise ValueError(name)
                return value
            try:
                external_policy = {
                    "publish_products": policy_bool("publish_products", False),
                    "include_unavailable": policy_bool("include_unavailable", True),
                    "default_available_stock": policy_stock("default_available_stock"),
                    "default_unavailable_stock": policy_stock("default_unavailable_stock"),
                    "reference_prefix": store.slug,
                }
            except (TypeError, ValueError) as exc:
                return Response({str(exc) or "external_policy": "Provide valid external catalog import choices."}, status=400)
        key = request.headers.get("Idempotency-Key", "").strip() or uuid.uuid4().hex
        if len(key) > 128: return Response({"idempotency_key": "Must not exceed 128 characters."}, status=400)
        policy_digest = json.dumps({"import_policy": external_policy or {}, "section_resolutions": section_resolutions}, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(raw + b"\0" + (variants_raw or b"") + b"\0" + policy_digest).hexdigest(); existing = CatalogImportOperation.objects.filter(store=store, idempotency_key=key).first()
        if existing:
            if existing.payload_hash != digest: return Response({"code": "idempotency_conflict", "detail": "This key was used for another file."}, status=409)
            return Response(_operation_data(existing))
        sections = {x.key: x.pk for x in CatalogSection.objects.filter(store=store)}
        if filename.endswith(".csv") and _has_native_variant_headers(raw):
            return Response({"file": "Variants.csv cannot be imported alone. Select Products.csv first and upload both files together."}, status=400)
        if filename.endswith(".json"):
            default_section = str(request.data.get("default_section") or "").strip()
            if default_section not in sections:
                return Response({"default_section": "Choose a section belonging to this store for products without source classification."}, status=400)
            rows, errors, schema = _parse_json(raw, sections, default_section, external_policy); source_format = "json"
        elif filename.endswith(".csv") and (variants_upload is not None or _has_native_product_headers(raw)):
            if variants_upload and "variant" not in variants_upload.name.lower():
                return Response({"variants_file": "The second native CSV must be Variants.csv."}, status=400)
            rows, errors, schema = _parse_native_csv_pair(raw, variants_raw, sections, store); source_format = "csv-pair" if variants_raw else "csv"
        else: rows, errors, schema = _parse_tabular(raw, filename, sections, store); source_format = "xlsx" if filename.endswith(".xlsx") else "csv"
        unresolved_sections = sorted({row["section"] for row in rows if row.get("section") and row.get("section_id") is None})
        pending_sections = []
        resolved_rows = set()
        for source_key in unresolved_sections:
            choice = str(section_resolutions.get(source_key) or "").strip()
            if not choice: continue
            if choice == "__create__":
                target_key = slugify(source_key)[:50]
                if not target_key:
                    return Response({"section_resolutions": f"Cannot create a section from {source_key!r}."}, status=400)
                if target_key in sections or any(item["key"] == target_key and item["source"] != source_key for item in pending_sections):
                    return Response({"section_resolutions": f"The generated section key {target_key!r} conflicts with another section."}, status=400)
                pending_sections.append({"source": source_key, "key": target_key, "label": source_key[:100]})
                target_id = None
            elif choice in sections:
                target_key, target_id = choice, sections[choice]
            else:
                return Response({"section_resolutions": f"Choose an existing section or create a new one for {source_key!r}."}, status=400)
            for row in rows:
                if row.get("section") == source_key and row.get("section_id") is None:
                    row["section"], row["section_id"] = target_key, target_id
                    resolved_rows.add(row.get("source_row"))
        if resolved_rows:
            errors = [error for error in errors if not (
                error.get("row") in resolved_rows and (
                    error.get("code") == "unknown_section" or
                    error.get("fields", {}).get("section") == "Unknown section key for this store."
                )
            )]
        remaining_sections = sorted({row["section"] for row in rows if row.get("section") and row.get("section_id") is None and not any(item["key"] == row["section"] for item in pending_sections)})
        result = {"format": source_format, "schema": schema, "unknown_sections": remaining_sections, "pending_sections": pending_sections}
        if schema == "external-products-json-v1":
            from store.models import StoreConfig
            result.update({
                "currency": StoreConfig.objects.get(store=store).currency,
                "external_policy": external_policy,
                "stock_policy": "merchant_selected_external_policy",
                "warnings": [
                    "Public JSON has no currency: prices will use the target store currency without conversion.",
                    "Exact inventory_quantity values are preserved; merchant-approved defaults apply only when quantities are missing.",
                    "A zero source weight is treated as unknown, not as a measured zero kilograms.",
                ],
            })
        if schema == TWO_SHEET_SCHEMA:
            result["preview"] = {
                "products": len(rows), "simple_products": sum(not row["uses_variants"] for row in rows),
                "variant_products": sum(bool(row["uses_variants"]) for row in rows),
                "variants": sum(len(row["variants"]) for row in rows if row["uses_variants"]),
                "stock": sum(item["quantity"] for row in rows for item in row["variants"]),
                "groups": [{"ref": row["ref"], "combinations": [item["attributes"] for item in row["variants"]],
                            "effective_prices": [item["price"] if item["price"] is not None else row["price"] for item in row["variants"]]}
                           for row in rows],
            }
        operation = CatalogImportOperation.objects.create(store=store, submitted_by=request.user, idempotency_key=key,
            payload_hash=digest, rows=rows, validation_errors=errors, result=result)
        return Response(_operation_data(operation), status=status.HTTP_201_CREATED)


class CatalogImportCommitView(APIView):
    @transaction.atomic
    def post(self, request, store_id, operation_id):
        store = _merchant_store(request.user, store_id)
        if not has_permission(request.user, store, "products.create"): raise PermissionDenied("Product creation access is required.")
        try: _enforce_import(store)
        except EntitlementDenied as exc: return Response(exc.as_dict(), status=status.HTTP_403_FORBIDDEN)
        operation = CatalogImportOperation.objects.select_for_update().filter(store=store, pk=operation_id).first()
        if operation is None: return Response({"detail": "Import not found."}, status=404)
        if operation.status == CatalogImportOperation.Status.COMMITTED: return Response(_operation_data(operation))
        if operation.validation_errors: return Response(_operation_data(operation), status=409)
        owner = get_user_model().objects.select_for_update().get(pk=store.members.get(role=StoreMember.Roles.OWNER).user_id)
        type(store).objects.select_for_update().get(pk=store.pk)
        progress = operation.result or {}
        for pending in progress.get("pending_sections") or []:
            CatalogSection.objects.get_or_create(
                store=store, key=pending["key"], defaults={"label": pending["label"], "is_visible": True},
            )
        section_ids = dict(CatalogSection.objects.filter(store=store).values_list("key", "id"))
        for row in operation.rows:
            if row.get("section_id") is None and row.get("section") in section_ids:
                row["section_id"] = section_ids[row["section"]]
        cursor = max(0, int(progress.get("processed") or 0))
        batch_rows = operation.rows[cursor:cursor + IMPORT_BATCH_SIZE]
        existing = {x.ref: x for x in Product.objects.select_for_update().filter(store=store)}
        imported_schema = (operation.result or {}).get("schema")
        if imported_schema in {TWO_SHEET_SCHEMA, "external-products-json-v1"}:
            conflict_rows = operation.rows if cursor == 0 else batch_rows
            conflicts = sorted(row["ref"] for row in conflict_rows if row["ref"] in existing)
            if conflicts: return Response({"detail": "Create-only import conflicts with existing products.", "refs": conflicts}, status=409)
            incoming_skus = [variant.get("sku") for row in (operation.rows if cursor == 0 else batch_rows) for variant in row["variants"] if variant.get("sku")]
            stored_skus = set(StockVariant.objects.select_for_update().filter(store=store, sku__in=incoming_skus).values_list("sku", flat=True))
            if stored_skus: return Response({"detail": "Variant SKUs now conflict with stored catalog data.", "skus": sorted(stored_skus)}, status=409)
        quota_rows = operation.rows if cursor == 0 else batch_rows
        create_count = sum(1 for row in quota_rows if row["ref"] not in existing)
        try:
            if create_count: enforce_operation(owner, "catalog.products.create", context={"store": store, "amount": create_count})
        except EntitlementDenied as exc: return Response(exc.as_dict(), status=403)
        created = updated = variants_count = 0
        try:
            for row in batch_rows:
                product = existing.get(row["ref"])
                if product is None: product = Product(store=store, ref=row["ref"]); created += 1
                else: updated += 1
                for field in ("name", "slug", "description", "price", "compare_at_price", "cost_price", "tags", "seo_title", "seo_description", "attributes", "extra", "variant_options", "uses_variants", "is_active", "is_featured", "track_inventory", "source_system", "source_id", "source_metadata"):
                    if field in row: setattr(product, field, row[field])
                product.section_id = row["section_id"]; product.full_clean(); product.save()
                product.categories.set(CatalogSection.objects.filter(store=store, key__in=row["categories"]))
                vendors = []
                for name in row["vendors"]:
                    vendor, _ = ProductVendor.objects.get_or_create(store=store, slug=slugify(name)[:120] or "vendor", defaults={"name": name}); vendors.append(vendor)
                product.vendors.set(vendors); product.variants.all().delete()
                media_by_url, media_by_source = {}, {}
                image_inputs = row.get("images") or [
                    {"source_id": hashlib.sha256(url.encode("utf-8")).hexdigest(), "url": url, "position": position + 1, "alt_text": ""}
                    for position, url in enumerate(row.get("image_urls") or [])
                ]
                for position, image_data in enumerate(sorted(image_inputs, key=lambda item: item.get("position") or 0)):
                    media = reference_product_image(
                        product=product, url=image_data["url"], order=position, is_primary=position == 0,
                        source_system=row.get("source_system") or "native_url",
                        source_id=str(image_data.get("source_id") or hashlib.sha256(image_data["url"].encode("utf-8")).hexdigest()),
                        alt_text=str(image_data.get("alt_text") or ""),
                    )
                    media_by_url[image_data["url"]] = media
                    media_by_source[str(image_data.get("source_id") or "")] = media
                definitions = [{
                    "code": item["key"], "name": item["label"], "kind": item.get("kind") or (
                        "color" if item.get("type") == "color" else "text"
                    ), "values": item.get("values", []),
                } for item in row.get("variant_options", [])]
                options = configure_product_options(product, definitions) if definitions and imported_schema in {TWO_SHEET_SCHEMA, "external-products-json-v1"} else []
                option_by_code = {item.code: item for item in options}
                for data in row["variants"]:
                    persisted = {key: value for key, value in data.items() if key in {"attributes", "price", "compare_at_price", "cost_price", "sku", "barcode", "weight", "quantity", "reserved", "extra", "is_active", "source_system", "source_id", "source_metadata"}}
                    persisted["image"] = media_by_source.get(str(data.get("source_image_id") or "")) or media_by_url.get(str(data.get("image_url") or ""))
                    variant = StockVariant(store=store, product=product, **persisted); variant.full_clean(); variant.save(); variants_count += 1
                    if options:
                        selections = []
                        for code, raw_value in data["attributes"].items():
                            option = option_by_code[code]
                            value = option.values.get(code__iexact=str(raw_value))
                            selections.append((option, value))
                        set_variant_selections(variant, selections)
                ensure_product_inventory_identity(product)
        except (DjangoValidationError, ValueError) as exc:
            transaction.set_rollback(True); return Response({"detail": "Import commit failed validation.", "errors": getattr(exc, "message_dict", str(exc))}, status=409)
        next_cursor = cursor + len(batch_rows)
        operation.status = CatalogImportOperation.Status.COMMITTED if next_cursor >= len(operation.rows) else CatalogImportOperation.Status.VALIDATED
        operation.result = {
            **progress,
            "created": int(progress.get("created") or 0) + created,
            "updated": int(progress.get("updated") or 0) + updated,
            "rejected": int(progress.get("rejected") or 0),
            "failed": int(progress.get("failed") or 0),
            "variants": int(progress.get("variants") or 0) + variants_count,
            "processed": next_cursor,
            "total": len(operation.rows),
        }
        operation.save(update_fields=["status", "result", "updated_at"]); return Response(_operation_data(operation))
