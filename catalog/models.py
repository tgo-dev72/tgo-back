"""Tenant-scoped commerce catalog with configurable retail attributes.

Architecture (top-down):
  RetailType        → master blueprint: sections, attrs, field visibility, units
  Store.retail_type → picks a RetailType at creation
  CatalogSection    → merchant-owned hierarchical categories
  AttributeDef      → per-section attribute schema (type, level, options)
  Product           → pricing, organization, SEO, visibility and relations
  StockVariant      → SKU, barcode, media, price and inventory identity
  ProductImage      → ordered image/external-video media
  ProductReview     → moderated customer feedback and rating aggregates

RetailType provides initial field recommendations. Store-owned attributes keep
the product shape adaptable without hiding operational commerce data in JSON.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models
from django.db.models import JSONField

from django.core.files.storage import storages
from django.utils.text import slugify

from store.models import Store


class CatalogImportOperation(models.Model):
    """Validated, retry-safe import staged before an explicit atomic commit."""

    class Status(models.TextChoices):
        VALIDATED = "validated", "Validated"
        COMMITTED = "committed", "Committed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="catalog_imports")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    rows = JSONField(default=list)
    validation_errors = JSONField(default=list)
    result = JSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VALIDATED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["store", "idempotency_key"], name="uniq_catalog_import_idempotency_per_store",
        )]


def product_image_storage():
    """Use Django's configured default storage.

    Keeping the field storage behind this callable preserves migration
    portability while allowing local validation to select filesystem storage
    even when a developer's environment also contains Cloudinary credentials.
    """
    return storages["default"]


# ─── Choices ──────────────────────────────────────────────────────────────────

SECTION_ICONS = [
    ("FaBoxOpen:fa", "Box"), ("FaTshirt:fa", "Shirt"), ("FaShoePrints:fa", "Shoe"),
    ("FaLeaf:fa", "Leaf"), ("FaGem:fa", "Gem"), ("FaHeart:fa", "Heart"),
    ("FaBagShopping:fa6", "Bag"), ("FaLaptop:fa", "Laptop"), ("FaCookieBite:fa", "Food"),
    ("FaPaintBrush:fa", "Art"), ("FaBook:fa", "Book"), ("FaCouch:fa", "Furniture"),
    ("FaFlask:fa", "Cosmetics"), ("FaWrench:fa", "Hardware"), ("FaMedal:fa", "Sports"),
    ("FaPaw:fa", "Pets"), ("FaCar:fa", "Auto"), ("FaBaby:fa", "Baby"),
]

ATTR_TYPES = [
    ("select", "Select"), ("multi", "Multi-select"), ("color", "Color"),
    ("text", "Text"), ("number", "Number"), ("boolean", "Boolean"), ("date", "Date"),
]

ATTR_LEVELS = [
    ("variant", "Variant (SKU)"), ("product", "Product (info)"),
]


# ─── RetailType: the master blueprint ─────────────────────────────────────────

class RetailType(models.Model):
    """Defines a retail vertical. Config shapes the entire product data model.

    config = {
        # Which Product columns are visible & required
        "product_fields": {
            "sku":           {"visible": True,  "required": False},
            "barcode":       {"visible": True,  "required": False},
            "weight":        {"visible": True,  "required": False},
            "unit":          {"visible": True,  "required": False},  # per-kg, per-meter
            "expiry_date":   {"visible": False, "required": False},
            "download_url":  {"visible": False, "required": False},
            "meta_title":    {"visible": True,  "required": False},
            "tags":          {"visible": True,  "required": False},
        },
        # Which StockVariant columns are visible & required
        "variant_fields": {
            "sku":           {"visible": True,  "required": False},
            "barcode":       {"visible": True,  "required": False},
            "expiry_date":   {"visible": False, "required": False},
            "batch_number":  {"visible": False, "required": False},
            "download_url":  {"visible": False, "required": False},
            "min_quantity":  {"visible": False, "required": False},
            "step":          {"visible": False, "required": False},
        },
        # Allowed selling units (empty = piece only)
        "units": ["piece"],
        # Feature flags
        "features": {
            "has_variants":      True,
            "has_batch_tracking": False,
            "has_digital":       False,
            "has_weight_based":  False,
        }
    }
    """
    key = models.SlugField(max_length=50, unique=True)
    label = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    icon = models.CharField(max_length=40, blank=True, default="")
    config = JSONField(default=dict, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "key"]

    def __str__(self):
        return self.label

    def product_fields_config(self):
        return self.config.get("product_fields", {})

    def variant_fields_config(self):
        return self.config.get("variant_fields", {})

    def allowed_units(self):
        return self.config.get("units", ["piece"])

    def features(self):
        return self.config.get("features", {})

    def clean(self):
        super().clean()
        validate_retail_type_config(self.config)


# ─── CatalogSection + AttributeDef ────────────────────────────────────────────

class CatalogSection(models.Model):
    class SortMode(models.TextChoices):
        MANUAL = "manual", "Manual"
        NEWEST = "newest", "Newest first"
        PRICE_ASC = "price_asc", "Price: low to high"
        PRICE_DESC = "price_desc", "Price: high to low"
        NAME_ASC = "name_asc", "Name: A to Z"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="catalog_sections")
    key = models.SlugField(max_length=50)
    label = models.CharField(max_length=100)
    label_translations = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True, default="")
    thumbnail = models.FileField(
        storage=product_image_storage,
        upload_to="documents/categories",
        blank=True,
        default="",
    )
    is_visible = models.BooleanField(default=True)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
    )
    sort_mode = models.CharField(max_length=16, choices=SortMode.choices, default=SortMode.MANUAL)
    icon = models.CharField(max_length=40, choices=SECTION_ICONS, default=SECTION_ICONS[0][0])
    sort_order = models.PositiveIntegerField(default=0)
    seo_title = models.CharField(max_length=70, blank=True, default="")
    seo_description = models.CharField(max_length=320, blank=True, default="")
    seo_social_image = models.URLField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["sort_order", "key"]
        constraints = [
            models.UniqueConstraint(fields=["store", "key"], name="unique_section_key_per_store"),
        ]

    def __str__(self):
        return f"{self.store.slug} / {self.key}"

    def clean(self):
        super().clean()
        if self.parent_id:
            if self.parent_id == self.pk:
                raise ValidationError({"parent": "A category cannot be its own parent."})
            if self.store_id and self.parent.store_id != self.store_id:
                raise ValidationError({"parent": "Parent category must belong to this store."})
            ancestor = self.parent
            depth = 1
            visited = {self.pk} if self.pk else set()
            while ancestor is not None:
                if ancestor.pk in visited:
                    raise ValidationError({"parent": "Category hierarchy cannot contain a cycle."})
                visited.add(ancestor.pk)
                depth += 1
                if depth > 3:
                    raise ValidationError({"parent": "Category hierarchy is limited to three levels."})
                ancestor = ancestor.parent


class AttributeDef(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="attribute_defs")
    section = models.ForeignKey(CatalogSection, on_delete=models.CASCADE, related_name="attribute_defs")
    key = models.SlugField(max_length=50)
    label = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=ATTR_TYPES, default="select")
    level = models.CharField(max_length=10, choices=ATTR_LEVELS, default="variant")
    options = JSONField(default=list, blank=True)
    required = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "key"]
        constraints = [
            models.UniqueConstraint(fields=["store", "section", "key"], name="unique_attr_key_per_section"),
        ]

    def __str__(self):
        return f"{self.section.key}.{self.key}"

    def clean(self):
        super().clean()
        if self.section_id and self.store_id and self.section.store_id != self.store_id:
            raise ValidationError({"section": "Section does not belong to this store."})
        if not isinstance(self.options, list):
            raise ValidationError({"options": "Options must be a list."})
        for option in self.options:
            if isinstance(option, dict):
                if not str(option.get("value", "")).strip():
                    raise ValidationError({"options": "Every option must have a non-empty value."})
            elif not str(option).strip():
                raise ValidationError({"options": "Options cannot contain empty values."})


# ─── Product: lean universal core ─────────────────────────────────────────────

class ProductVendor(models.Model):
    """A reusable vendor/brand label scoped to one merchant store."""

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_vendors")
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(fields=["store", "slug"], name="uniq_product_vendor_slug_per_store"),
        ]

    def __str__(self):
        return f"{self.store.slug} / {self.name}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:110] or "vendor"
            candidate = base
            suffix = 2
            while ProductVendor.objects.filter(store_id=self.store_id, slug=candidate).exclude(pk=self.pk).exists():
                tail = f"-{suffix}"
                candidate = f"{base[:120 - len(tail)]}{tail}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)

class Product(models.Model):
    """Universal commerce product plus validated store-specific attributes."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    section = models.ForeignKey(CatalogSection, on_delete=models.PROTECT, related_name="products")

    # ── Universal (always visible) ─────────────────────────────────────────
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, blank=True, default="")
    ref = models.CharField(max_length=100)
    source_system = models.CharField(max_length=32, blank=True, default="")
    source_id = models.CharField(max_length=100, blank=True, default="")
    source_metadata = JSONField(default=dict, blank=True)
    description = models.TextField(blank=True, default="")
    price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"),
                                validators=[MinValueValidator(Decimal("0.00"))])
    compare_at_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    promo = models.FloatField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])
    loyalty_points = models.PositiveIntegerField(default=0)
    rating = models.FloatField(default=0, validators=[MinValueValidator(0), MaxValueValidator(5)])
    reviews_count = models.PositiveIntegerField(default=0)

    # ── Config-driven (shaped by RetailType.config) ────────────────────────
    # Everything type-specific goes here: sku, barcode, weight, weight_unit,
    # length, width, height, dimension_unit, unit, unit_price,
    # product_type, meta_title, meta_description, tags,
    # expiry_date (product-level), download_url (product-level), …
    extra = JSONField(default=dict, blank=True,
                      help_text="Type-specific fields from RetailType.config.")

    # ── Merchant-defined attributes (Material, Fit, Brand, etc.) ───────────
    attributes = JSONField(default=dict, blank=True)
    variant_options = JSONField(
        default=list,
        blank=True,
        help_text="Product-specific customer choices such as color, size or material.",
    )
    tags = JSONField(default=list, blank=True)
    seo_title = models.CharField(max_length=255, blank=True, default="")
    seo_description = models.TextField(blank=True, default="")
    seo_social_image = models.URLField(max_length=500, blank=True, default="")
    categories = models.ManyToManyField(
        CatalogSection,
        blank=True,
        related_name="catalog_products",
        help_text="Additional storefront categories; section remains the primary schema category.",
    )
    vendors = models.ManyToManyField(ProductVendor, blank=True, related_name="products")
    related_products = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=False,
        related_name="recommended_by",
    )

    # ── Status ─────────────────────────────────────────────────────────────
    is_active = models.BooleanField(default=True)
    uses_variants = models.BooleanField(
        default=False,
        help_text="Merchant choice: customers select an option combination for this product.",
    )
    show_related_products = models.BooleanField(
        default=False,
        help_text="Merchant choice: show recommendations on this product page.",
    )
    track_inventory = models.BooleanField(default=True)
    is_featured = models.BooleanField(
        default=False,
        help_text="Merchant-controlled inclusion in featured homepage selections.",
    )
    merchandising_order = models.PositiveIntegerField(
        default=0,
        help_text="Lower values appear first when products are manually featured.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "ref"], name="unique_product_ref_per_store"),
            models.UniqueConstraint(
                fields=["store", "slug"],
                condition=~models.Q(slug=""),
                name="unique_product_slug_per_store",
            ),
            models.UniqueConstraint(
                fields=["store", "source_system", "source_id"],
                condition=~models.Q(source_system="") & ~models.Q(source_id=""),
                name="uniq_product_external_identity",
            ),
        ]

    def __str__(self):
        return f"{self.ref} - {self.name}"

    def clean(self):
        super().clean()
        if self.section_id and self.store_id and self.section.store_id != self.store_id:
            raise ValidationError({"section": "Section does not belong to this store."})
        self.attributes = validate_attributes(
            self.attributes, self.store, self.section_id, level="product",
        )
        self.variant_options = validate_product_variant_options(self.variant_options)
        self.extra = validate_extra_fields(self.extra, self.store.retail_type, level="product", config=self.store.catalog_config.get("product_fields", {}))
        if not isinstance(self.tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in self.tags):
            raise ValidationError({"tags": "Tags must be a list of non-empty text values."})
        self.tags = list(dict.fromkeys(tag.strip() for tag in self.tags))
        if self.compare_at_price is not None and self.compare_at_price <= self.price:
            raise ValidationError({"compare_at_price": "Compare-at price must be greater than the selling price."})

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        relationship_changed = (
            self._state.adding
            or update_fields is None
            or bool({"store", "store_id", "section", "section_id"} & set(update_fields))
        )
        if relationship_changed and self.section_id and self.store_id:
            section_store_id = CatalogSection.objects.filter(pk=self.section_id).values_list("store_id", flat=True).first()
            if section_store_id != self.store_id:
                raise ValidationError({"section": "Section does not belong to this store."})
        if not self.slug:
            base = slugify(self.name)[:240] or "product"
            candidate = base
            suffix = 2
            while Product.objects.filter(store_id=self.store_id, slug=candidate).exclude(pk=self.pk).exists():
                tail = f"-{suffix}"
                candidate = f"{base[:255 - len(tail)]}{tail}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)

    @property
    def inventory(self):
        if not self.track_inventory:
            return None
        return sum(variant.available_quantity() for variant in self.variants.all())

    @property
    def you_save_amount(self):
        if self.compare_at_price is not None and self.compare_at_price > self.price:
            return self.compare_at_price - self.price
        discount = (self.price * Decimal(str(self.promo or 0)) / Decimal("100"))
        return discount.quantize(Decimal("0.01"))


class ProductSlugRedirect(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_slug_redirects")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="slug_redirects")
    old_slug = models.SlugField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "old_slug"], name="uniq_product_old_slug_per_store")]


class CatalogSectionSlugRedirect(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="section_slug_redirects")
    section = models.ForeignKey(CatalogSection, on_delete=models.CASCADE, related_name="slug_redirects")
    old_slug = models.SlugField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["store", "old_slug"], name="uniq_section_old_slug_per_store")]


class ProductImage(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        EXTERNAL_VIDEO = "external_video", "External video"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="images")
    image = models.FileField(storage=product_image_storage, upload_to="documents/products", blank=True)
    media_type = models.CharField(max_length=16, choices=MediaType.choices, default=MediaType.IMAGE)
    external_url = models.URLField(max_length=1000, blank=True, default="")
    source_system = models.CharField(max_length=32, blank=True, default="")
    source_id = models.CharField(max_length=100, blank=True, default="")
    source_metadata = JSONField(default=dict, blank=True)
    source_url = models.URLField(max_length=1000, blank=True, default="")
    alt_text = models.CharField(max_length=255, blank=True, default="")
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        constraints = [models.UniqueConstraint(
            fields=["product"], condition=models.Q(is_primary=True),
            name="uniq_primary_media_per_product",
        ), models.UniqueConstraint(
            fields=["product", "source_system", "source_id"],
            condition=~models.Q(source_system="") & ~models.Q(source_id=""),
            name="uniq_product_media_external_identity",
        )]

    def __str__(self):
        return f"{self.product.name} - image {self.order}"

    def clean(self):
        super().clean()
        if self.media_type == self.MediaType.IMAGE and not self.image and not self.external_url:
            raise ValidationError({"image": "Upload an image."})
        if self.media_type == self.MediaType.EXTERNAL_VIDEO and not self.external_url:
            raise ValidationError({"external_url": "Provide the external video URL."})
        if self.is_primary and self.media_type != self.MediaType.IMAGE:
            raise ValidationError({"is_primary": "Only an image can be the product cover."})


# ─── StockVariant: lean SKU core ──────────────────────────────────────────────

class ProductOption(models.Model):
    """Stable identity for one sellable product dimension."""

    class Kind(models.TextChoices):
        COLOR = "color", "Color"
        SIZE_NUMERIC = "size_numeric", "Numeric size"
        SIZE_ALPHA = "size_alpha", "Alphabetic size"
        TEXT = "text", "Text"

    class Presentation(models.TextChoices):
        SWATCHES = "swatches", "Swatches"
        BUTTONS = "buttons", "Buttons"
        DROPDOWN = "dropdown", "Dropdown"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_options")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="options")
    code = models.SlugField(max_length=50)
    label = models.CharField(max_length=100)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    presentation = models.CharField(max_length=20, choices=Presentation.choices, default=Presentation.DROPDOWN)
    position = models.PositiveSmallIntegerField(default=0)
    required = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["product", "code"], name="uniq_option_code_per_product"),
            models.UniqueConstraint(fields=["product", "position"], name="uniq_option_position_per_product"),
        ]

    def clean(self):
        super().clean()
        if self.product_id and self.store_id and self.product.store_id != self.store_id:
            raise ValidationError({"product": "Option product does not belong to this store."})


class ProductOptionValue(models.Model):
    """Ordered value owned by exactly one product option."""

    option = models.ForeignKey(ProductOption, on_delete=models.CASCADE, related_name="values")
    code = models.CharField(max_length=100)
    label = models.CharField(max_length=100)
    swatch = models.CharField(max_length=7, blank=True, default="")
    position = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["option", "code"], name="uniq_option_value_code"),
            models.UniqueConstraint(fields=["option", "position"], name="uniq_option_value_position"),
        ]

    def clean(self):
        super().clean()
        self.code, self.label = str(self.code).strip(), str(self.label).strip()
        if not self.code or not self.label:
            raise ValidationError({"code": "Option value code and label are required."})
        if self.swatch:
            self.swatch = self.swatch.lower()
            if self.option.kind != ProductOption.Kind.COLOR:
                raise ValidationError({"swatch": "Only color values may define a swatch."})
            if not re.fullmatch(r"#[0-9a-f]{6}", self.swatch):
                raise ValidationError({"swatch": "Use a six-digit hexadecimal color."})


class StockVariant(models.Model):
    """Lean variant — type-specific data in `extra` JSON."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="stock_variants")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="variants")

    # ── Config-driven attributes (color, size, storage, etc.) ──────────────
    attributes = JSONField(default=dict, blank=True)

    # ── Pricing & stock (always) ───────────────────────────────────────────
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                validators=[MinValueValidator(Decimal("0.00"))])
    compare_at_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    cost_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    sku = models.CharField(max_length=100, blank=True, default="")
    source_system = models.CharField(max_length=32, blank=True, default="")
    source_id = models.CharField(max_length=100, blank=True, default="")
    source_metadata = JSONField(default=dict, blank=True)
    barcode = models.CharField(max_length=100, blank=True, default="")
    weight = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    image = models.ForeignKey(
        ProductImage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="variants",
    )
    quantity = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)

    # ── Config-driven extra (sku, barcode, expiry_date, batch_number,
    #    download_url, min_quantity, step, …) ────────────────────────────────
    extra = JSONField(default=dict, blank=True)
    combination_key = models.CharField(max_length=64, blank=True, default="", editable=False)
    is_active = models.BooleanField(default=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(reserved__lte=models.F("quantity")),
                name="variant_reserved_not_above_quantity",
            ),
            models.UniqueConstraint(
                fields=["store", "sku"],
                condition=~models.Q(sku=""),
                name="uniq_variant_sku_per_store",
            ),
            models.UniqueConstraint(
                fields=["store", "barcode"],
                condition=~models.Q(barcode=""),
                name="uniq_variant_barcode_per_store",
            ),
            models.UniqueConstraint(
                fields=["product", "combination_key"],
                condition=~models.Q(combination_key=""),
                name="uniq_variant_combination_per_product",
            ),
            models.UniqueConstraint(
                fields=["store", "source_system", "source_id"],
                condition=~models.Q(source_system="") & ~models.Q(source_id=""),
                name="uniq_variant_external_identity",
            ),
        ]

    def available_quantity(self):
        return max(0, self.quantity - self.reserved)

    def __str__(self):
        return f"{self.product.ref} {self.attributes}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        relationship_changed = (
            self._state.adding
            or update_fields is None
            or bool({"store", "store_id", "product", "product_id", "image", "image_id"} & set(update_fields))
        )
        if relationship_changed and self.product_id and self.store_id:
            product_store_id = Product.objects.filter(pk=self.product_id).values_list("store_id", flat=True).first()
            if product_store_id != self.store_id:
                raise ValidationError({"product": "Product does not belong to this store."})
        if relationship_changed and self.image_id and self.product_id:
            image_product_id = ProductImage.objects.filter(pk=self.image_id).values_list("product_id", flat=True).first()
            if image_product_id != self.product_id:
                raise ValidationError({"image": "Variant image must belong to this product."})
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.product_id and self.store_id and self.product.store_id != self.store_id:
            raise ValidationError({"product": "Product does not belong to this store."})
        section_id = self.product.section_id if self.product_id else None
        if self.product_id and is_default_inventory_variant(self):
            # Category options are suggestions for configurable products;
            # the system stock row of a simple product has no shopper choices.
            pass
        elif self.product_id and self.product.variant_options:
            self.attributes = validate_product_variant_attributes(self.attributes, self.product.variant_options)
        else:
            self.attributes = validate_attributes(
                self.attributes, self.store, section_id, level="variant",
            )
        if self.product_id:
            self.attributes = validate_variant_sizing(self.attributes, self.product)
        if self.image_id and self.product_id and self.image.product_id != self.product_id:
            raise ValidationError({"image": "Variant image must belong to this product."})
        self.extra = validate_extra_fields(self.extra, self.store.retail_type, level="variant", config=self.store.catalog_config.get("variant_fields", {}))


class VariantSelection(models.Model):
    """One stable option/value pair participating in a variant combination."""

    variant = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="selections")
    option = models.ForeignKey(ProductOption, on_delete=models.PROTECT, related_name="variant_selections")
    value = models.ForeignKey(ProductOptionValue, on_delete=models.PROTECT, related_name="variant_selections")

    class Meta:
        ordering = ["option__position", "option_id"]
        constraints = [models.UniqueConstraint(
            fields=["variant", "option"], name="uniq_selection_option_per_variant",
        )]

    def clean(self):
        super().clean()
        errors = {}
        if self.variant_id and self.option_id and self.option.product_id != self.variant.product_id:
            errors["option"] = "Selected option does not belong to the variant product."
        if self.value_id and self.option_id and self.value.option_id != self.option_id:
            errors["value"] = "Selected value does not belong to the selected option."
        if errors:
            raise ValidationError(errors)


class ProductReview(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_reviews")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="review_entries")
    customer_name = models.CharField(max_length=100)
    customer_email = models.EmailField(blank=True, default="")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    title = models.CharField(max_length=160, blank=True, default="")
    content = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    verified_purchase = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def clean(self):
        super().clean()
        if self.product_id and self.store_id and self.product.store_id != self.store_id:
            raise ValidationError({"product": "Product does not belong to this store."})

    def _refresh_product_summary(self):
        summary = self.product.review_entries.filter(
            status=self.Status.PUBLISHED,
        ).aggregate(count=models.Count("id"), average=models.Avg("rating"))
        Product.objects.filter(pk=self.product_id).update(
            reviews_count=summary["count"] or 0,
            rating=float(summary["average"] or 0),
        )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self._refresh_product_summary()

    def delete(self, *args, **kwargs):
        product = self.product
        result = super().delete(*args, **kwargs)
        summary = product.review_entries.filter(
            status=self.Status.PUBLISHED,
        ).aggregate(count=models.Count("id"), average=models.Avg("rating"))
        Product.objects.filter(pk=product.pk).update(
            reviews_count=summary["count"] or 0,
            rating=float(summary["average"] or 0),
        )
        return result


def section_requires_configured_variants(section):
    """Return whether a product needs merchant-supplied SKU options."""
    return AttributeDef.objects.filter(
        section=section,
        level="variant",
        required=True,
    ).exists()


def is_default_inventory_variant(variant):
    """Identify the system-created stock row used by a simple product."""
    return (
        variant.attributes == {}
        and not variant.product.uses_variants
    )


def ensure_product_inventory_identity(product):
    """Give a simple product a stock row, or keep a configurable product draft.

    Product creation and variant creation are separate dashboard requests. A
    configurable product therefore starts as a draft until its first valid
    SKU is supplied; a simple product receives one zero-stock default SKU.
    """
    if product.uses_variants:
        if product.variants.exclude(attributes={}).exists():
            return True
        if product.is_active:
            Product.objects.filter(pk=product.pk).update(is_active=False)
            product.is_active = False
        return False
    if product.variants.exists():
        return True
    StockVariant.objects.create(
        store=product.store,
        product=product,
        attributes={},
        quantity=0,
        reserved=0,
        extra={},
    )
    return True


def attribute_key_in_use(section, key, level=None):
    """Check persisted JSON before allowing a schema key to be removed."""
    querysets = []
    if level in {None, "product"}:
        querysets.append(Product.objects.filter(section=section).values_list("attributes", flat=True))
    if level in {None, "variant"}:
        querysets.append(
            StockVariant.objects.filter(product__section=section).values_list("attributes", flat=True)
        )
    return any(
        isinstance(attributes, dict) and key in attributes
        for queryset in querysets
        for attributes in queryset.iterator()
    )


def attribute_scope_has_records(section, level):
    """Check whether making an attribute required would invalidate records."""
    if level == "product":
        return Product.objects.filter(section=section).exists()
    return StockVariant.objects.filter(product__section=section).exists()


# ─── Config-driven validation ─────────────────────────────────────────────────

def _is_empty(value):
    return value is None or value == "" or value == []


def validate_attributes(attributes, store, section_id=None, level=None):
    """Validate and normalize merchant-defined product/variant attributes."""
    if not isinstance(attributes, dict):
        raise ValidationError({"attributes": "Attributes must be an object."})

    qs = AttributeDef.objects.filter(store=store)
    if section_id:
        qs = qs.filter(section_id=section_id)
    if level:
        qs = qs.filter(level=level)
    defs = {d.key: d for d in qs}

    errors = {}
    all_qs = AttributeDef.objects.filter(store=store)
    if section_id:
        all_qs = all_qs.filter(section_id=section_id)
    all_defs = {d.key: d for d in all_qs}

    for key in attributes:
        if key not in defs:
            other = all_defs.get(key)
            if other and level and other.level != level:
                correct = "product" if level == "variant" else "variant"
                errors[key] = [f"Attribute '{key}' is {other.level}-level — set it on the {correct}."]
            else:
                errors[key] = [f"Unknown attribute '{key}'."]

    for key, d in defs.items():
        if d.required and (key not in attributes or _is_empty(attributes[key])):
            errors[key] = ["Required."]

    def _option_values(options):
        out = []
        for opt in options or []:
            if isinstance(opt, dict):
                value = opt.get("value")
                if value is not None:
                    out.append(value)
            else:
                out.append(opt)
        return out

    normalized = dict(attributes)
    for key, value in attributes.items():
        d = defs.get(key)
        if d is None:
            continue
        if not d.required and _is_empty(value):
            continue
        if d.type == "multi":
            if not isinstance(value, list):
                errors[key] = ["Must be a list of values."]
                continue
            if d.options:
                allowed = set(_option_values(d.options))
                bad = [v for v in value if v not in allowed]
                if bad:
                    errors[key] = [f"Invalid value(s) {bad}. Allowed: {sorted(allowed)}"]
        elif d.type in ("select", "color"):
            if isinstance(value, (list, dict)):
                errors[key] = ["Must be a single value."]
                continue
            if not d.options:
                continue
            allowed = set(_option_values(d.options))
            if value not in allowed:
                errors[key] = [f"Invalid value '{value}'. Allowed: {sorted(allowed)}"]
        elif d.type == "number":
            if isinstance(value, bool):
                errors[key] = ["Must be a number."]
                continue
            try:
                number = Decimal(str(value))
                normalized[key] = int(number) if number == number.to_integral_value() else float(number)
            except (InvalidOperation, TypeError, ValueError):
                errors[key] = ["Must be a number."]
        elif d.type == "boolean":
            if isinstance(value, bool):
                normalized[key] = value
            elif value in ("true", "1", 1):
                normalized[key] = True
            elif value in ("false", "0", 0):
                normalized[key] = False
            else:
                errors[key] = ["Must be true/false."]
        elif d.type == "date":
            try:
                normalized[key] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError):
                errors[key] = ["Must be a valid date (YYYY-MM-DD)."]
        elif d.type == "text" and not isinstance(value, str):
            errors[key] = ["Must be text."]

    if errors:
        raise ValidationError(errors)
    return normalized


def validate_product_variant_options(options):
    """Validate the merchant-owned choice schema stored on one product."""
    if not isinstance(options, list):
        raise ValidationError({"variant_options": "Product choices must be a list."})
    if len(options) > 3:
        raise ValidationError({"variant_options": "A product can have at most three customer choices."})
    normalized = []
    seen = set()
    allowed_types = {"select", "color", "text"}
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            raise ValidationError({"variant_options": f"Choice {index + 1} must be an object."})
        label = str(option.get("label", "")).strip()
        key = slugify(str(option.get("key") or label)).replace("-", "_")[:50]
        option_type = str(option.get("type", "select"))
        values = option.get("values", [])
        if not label or not key:
            raise ValidationError({"variant_options": f"Choice {index + 1} needs a name."})
        if key in seen:
            raise ValidationError({"variant_options": f"Choice name '{label}' is duplicated."})
        if option_type not in allowed_types:
            raise ValidationError({"variant_options": f"Choice '{label}' has an unsupported display style."})
        if not isinstance(values, list):
            raise ValidationError({"variant_options": f"Choice '{label}' values must be a list."})
        if len(values) > 100:
            raise ValidationError({"variant_options": f"Choice '{label}' can have at most 100 values."})
        clean_values = []
        seen_values = set()
        for raw_value in values:
            value_metadata = raw_value if isinstance(raw_value, dict) else None
            value = str(
                value_metadata.get("value") or value_metadata.get("label") or ""
                if value_metadata is not None else raw_value
            ).strip()
            if not value:
                continue
            if len(value) > 100:
                raise ValidationError({"variant_options": f"Values for '{label}' can have at most 100 characters."})
            value_key = value.casefold()
            if value_key in seen_values:
                raise ValidationError({"variant_options": f"Choice '{label}' contains the same value more than once."})
            seen_values.add(value_key)
            if value_metadata is not None:
                value_label = str(value_metadata.get("label") or value).strip()
                if len(value_label) > 100:
                    raise ValidationError({"variant_options": f"Labels for '{label}' can have at most 100 characters."})
                swatch = str(value_metadata.get("swatch") or "").strip().lower()
                if option_type != "color":
                    clean_values.append(value)
                    continue
                if not re.fullmatch(r"#[0-9a-f]{6}", swatch):
                    raise ValidationError({"variant_options": f"Color '{value_label}' needs a valid six-digit color value."})
                clean_values.append({"value": value, "label": value_label, "swatch": swatch})
            else:
                clean_values.append(value)
        if not clean_values:
            raise ValidationError({"variant_options": f"Add at least one value for '{label}'."})
        seen.add(key)
        normalized.append({"key": key, "label": label, "type": option_type, "values": clean_values})
    return normalized


def validate_product_variant_attributes(attributes, options):
    """Validate one SKU combination against its product-owned choices."""
    if not isinstance(attributes, dict):
        raise ValidationError({"attributes": "Attributes must be an object."})
    definitions = {option["key"]: option for option in validate_product_variant_options(options)}
    errors = {}
    for key in attributes:
        if key not in definitions:
            errors[key] = [f"Unknown product choice '{key}'."]
    normalized = {}
    for key, option in definitions.items():
        value = str(attributes.get(key, "")).strip()
        if not value:
            errors[key] = ["Required."]
        else:
            configured_values = {}
            for candidate in option["values"]:
                canonical = (
                    str(candidate.get("value") or candidate.get("label") or "")
                    if isinstance(candidate, dict) else str(candidate)
                )
                configured_values[canonical.casefold()] = canonical
            if value.casefold() in configured_values:
                normalized[key] = configured_values[value.casefold()]
            else:
                allowed = [
                    str(candidate.get("label") or candidate.get("value") or "")
                    if isinstance(candidate, dict) else str(candidate)
                    for candidate in option["values"]
                ]
                errors[key] = [f"Choose one of: {', '.join(allowed)}."]
    if errors:
        raise ValidationError(errors)
    return normalized


def validate_variant_sizing(attributes, product):
    """Apply a product's sizing policy to its SKU-level size value.

    Attribute definitions describe the available fields. The product decides
    whether its own size values are letters or numbers, allowing both systems
    inside the same clothing catalog without changing the section schema.
    """
    sizing_system = str((product.attributes or {}).get("size_system", "")).strip().lower()
    if sizing_system not in {"letters", "numeric"} or _is_empty(attributes.get("size")):
        return attributes

    normalized = dict(attributes)
    raw_size = str(attributes["size"]).strip()
    if sizing_system == "numeric":
        try:
            number = Decimal(raw_size)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValidationError({"size": ["Use a numeric size, such as 36 or 42.5."]}) from exc
        if not number.is_finite() or number <= 0:
            raise ValidationError({"size": ["Use a positive numeric size."]})
        canonical = format(number, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        normalized["size"] = canonical
    else:
        try:
            Decimal(raw_size)
        except (InvalidOperation, TypeError, ValueError):
            normalized["size"] = raw_size.upper()
        else:
            raise ValidationError({"size": ["Use a letter size, such as S, M, XL or 2Y."]})
    return normalized


def validate_retail_type_config(config):
    """Validate the schema that drives dashboard product fields."""
    from .retail_presets import EXTRA_FIELD_TYPES

    if not isinstance(config, dict):
        raise ValidationError({"config": "Retail type config must be an object."})
    errors = {}
    allowed_types = {"text", "number", "select", "date", "url", "string_list"}
    for group in ("product_fields", "variant_fields"):
        fields = config.get(group, {})
        if not isinstance(fields, dict):
            errors[group] = ["Must be an object keyed by field name."]
            continue
        for key, field_config in fields.items():
            if key not in EXTRA_FIELD_TYPES:
                errors.setdefault(group, []).append(f"Unknown configured field '{key}'.")
                continue
            if not isinstance(field_config, dict):
                errors.setdefault(group, []).append(f"{key} must be an object.")
                continue
            field_type = field_config.get("type", "text")
            if field_type not in allowed_types:
                errors.setdefault(group, []).append(f"{key} has unsupported type '{field_type}'.")
            for flag in ("visible", "required"):
                if flag in field_config and not isinstance(field_config[flag], bool):
                    errors.setdefault(group, []).append(f"{key}.{flag} must be a boolean.")
            choices = field_config.get("choices")
            if choices is not None and (
                not isinstance(choices, list) or not all(isinstance(choice, str) for choice in choices)
            ):
                errors.setdefault(group, []).append(f"{key}.choices must be a list of text values.")
    units = config.get("units", ["piece"])
    if not isinstance(units, list) or not all(isinstance(unit, str) and unit for unit in units):
        errors["units"] = ["Must be a list of unit names."]
    features = config.get("features", {})
    if not isinstance(features, dict) or not all(isinstance(value, bool) for value in features.values()):
        errors["features"] = ["Feature values must be booleans."]
    if errors:
        messages = [
            f"{group}: {message}"
            for group, group_errors in errors.items()
            for message in group_errors
        ]
        raise ValidationError({"config": messages})
    return config


def validate_extra_fields(extra, retail_type, level="product", *, config=None):
    """Validate operational data against the store snapshot (or a legacy preset)."""
    if not isinstance(extra, dict):
        raise ValidationError({"extra": "Extra must be an object."})
    if config is None:
        if not retail_type:
            return extra
        config = retail_type.product_fields_config() if level == "product" else retail_type.variant_fields_config()
    errors = {}
    normalized = dict(extra)
    url_validator = URLValidator()
    for key, value in extra.items():
        field_cfg = config.get(key)
        if field_cfg is None:
            errors[key] = [f"Unknown field for this retail type."]
            continue
        if _is_empty(value):
            continue
        if field_cfg.get("enabled", True) is False:
            errors[key] = ["This field is not enabled for this store."]
            continue
        field_type = field_cfg.get("type", "text")
        choices = field_cfg.get("choices")
        if choices and value not in choices:
            errors[key] = [f"Invalid value '{value}'. Allowed: {choices}"]
        elif field_type == "number":
            if isinstance(value, bool):
                errors[key] = ["Must be a number."]
                continue
            try:
                number = Decimal(str(value))
                if number < 0:
                    raise ValueError
                normalized[key] = int(number) if number == number.to_integral_value() else float(number)
            except (InvalidOperation, TypeError, ValueError):
                errors[key] = ["Must be a non-negative number."]
        elif field_type == "date":
            try:
                normalized[key] = date.fromisoformat(value).isoformat()
            except (TypeError, ValueError):
                errors[key] = ["Must be a valid date (YYYY-MM-DD)."]
        elif field_type == "url":
            try:
                url_validator(value)
            except ValidationError:
                errors[key] = ["Must be a valid URL."]
        elif field_type == "string_list":
            if isinstance(value, str):
                normalized[key] = [item.strip() for item in value.split(",") if item.strip()]
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                normalized[key] = value
            else:
                errors[key] = ["Must be a list of text values."]
        elif field_type in ("text", "select") and not isinstance(value, str):
            errors[key] = ["Must be text."]
    for key, field_cfg in config.items():
        if field_cfg.get("required") and (key not in extra or _is_empty(extra[key])):
            errors[key] = ["This field is required for this store."]
    if errors:
        raise ValidationError(errors)
    return normalized
