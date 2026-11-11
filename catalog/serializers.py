from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from access_control.services import effective_fields, membership_for, require_fields
from common.media_uploads import image_folder, media_asset_url, validate_cloudinary_upload

from .rich_text import sanitize_product_description
from .variant_services import configure_product_options, set_variant_selections
from .models import (
    AttributeDef, CatalogSection, Product, ProductImage,
    ProductReview, ProductVendor, RetailType, StockVariant,
    attribute_key_in_use, attribute_scope_has_records,
    ensure_product_inventory_identity, is_default_inventory_variant,
    validate_attributes, validate_extra_fields, validate_product_variant_attributes,
    validate_product_variant_options,
    validate_variant_sizing,
)

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ATTRIBUTE_SCHEMA_FIELDS = ("key", "type", "level", "options")


def _validate_raster_image(upload, *, max_size=MAX_UPLOAD_SIZE):
    """Verify image bytes instead of trusting the browser's MIME label."""
    if getattr(upload, "content_type", "") not in ALLOWED_IMAGE_TYPES:
        raise serializers.ValidationError("Choose a JPEG, PNG, WebP or GIF image.")
    if upload.size > max_size:
        raise serializers.ValidationError(f"File too large ({upload.size} bytes). Max: {max_size}.")
    try:
        verified = serializers.ImageField().run_validation(upload)
    except serializers.ValidationError as exc:
        raise serializers.ValidationError("The uploaded file is not a valid image.") from exc
    finally:
        if hasattr(upload, "seek"):
            upload.seek(0)
    return verified


def _validate_store_identifiers(store, extra, instance=None):
    """Keep non-empty SKU/barcode values unique across a store's catalog."""
    errors = {}
    for key in ("sku", "barcode"):
        value = extra.get(key)
        if not value:
            continue
        products = Product.objects.filter(store=store, **{f"extra__{key}": value})
        variants = StockVariant.objects.filter(store=store, **{f"extra__{key}": value})
        if isinstance(instance, Product):
            products = products.exclude(pk=instance.pk)
        elif isinstance(instance, StockVariant):
            variants = variants.exclude(pk=instance.pk)
        if products.exists() or variants.exists():
            errors[key] = [f"This {key} is already used in this store."]
    if errors:
        raise serializers.ValidationError({"extra": errors})


def _validate_variant_identifiers(store, attrs, instance=None):
    errors = {}
    for key in ("sku", "barcode"):
        value = str(attrs.get(key, getattr(instance, key, "") if instance else "") or "").strip()
        if not value:
            continue
        variants = StockVariant.objects.filter(store=store, **{key: value})
        if instance is not None:
            variants = variants.exclude(pk=instance.pk)
        if variants.exists():
            errors[key] = f"This {key} is already used in this store."
    if errors:
        raise serializers.ValidationError(errors)


def _asset_url(field, context):
    api_base = getattr(settings, "STOREFRONT_API_URL", "") or ""
    return media_asset_url(field, request=context.get("request"), base_url=api_base)


def _sync_simple_variant_identity(product):
    variants = list(product.variants.all()[:2])
    if len(variants) != 1 or variants[0].attributes:
        return
    variant = variants[0]
    extra = product.extra if isinstance(product.extra, dict) else {}
    updates = []
    for field in ("sku", "barcode", "weight"):
        value = extra.get(field)
        if value in (None, ""):
            continue
        setattr(variant, field, value)
        updates.append(field)
    if updates:
        variant.save(update_fields=[*updates, "updated_at"])


def _sync_product_option_records(product):
    definitions = []
    for item in product.variant_options or []:
        values = item.get("values") or []
        raw_values = [str(value.get("value") if isinstance(value, dict) else value) for value in values]
        kind = item.get("kind")
        if kind not in {"color", "size_numeric", "size_alpha", "text"}:
            if item.get("type") == "color": kind = "color"
            elif item.get("key") == "size" and raw_values and all(value.isdigit() for value in raw_values): kind = "size_numeric"
            elif item.get("key") == "size" and raw_values: kind = "size_alpha"
            else: kind = "text"
        definitions.append({"code": item.get("key"), "name": item.get("label"), "kind": kind, "values": values})
    if definitions:
        return configure_product_options(product, definitions)
    product.options.filter(is_active=True).update(is_active=False)
    Product.objects.filter(pk=product.pk).update(variant_options=[])
    return []


def _sync_variant_selection_records(variant):
    options = {option.code: option for option in variant.product.options.filter(is_active=True).prefetch_related("values")}
    if not options:
        return variant
    selections = []
    for code, raw in variant.attributes.items():
        option = options.get(code)
        if option is None:
            continue
        value = option.values.filter(code__iexact=str(raw), is_active=True).first()
        if value is None:
            raise serializers.ValidationError({"attributes": {code: "Choose a configured product option value."}})
        selections.append((option, value))
    try:
        return set_variant_selections(variant, selections)
    except Exception as exc:
        raise serializers.ValidationError(getattr(exc, "message_dict", {"attributes": str(exc)})) from exc


def _validate_attribute_definition_change(instance, attrs):
    """Require an explicit data cleanup before changing an in-use schema."""
    errors = {}
    schema_changed = any(
        field in attrs and attrs[field] != getattr(instance, field)
        for field in ATTRIBUTE_SCHEMA_FIELDS
    )
    if schema_changed and attribute_key_in_use(instance.section, instance.key):
        errors["schema"] = (
            "This attribute is used by existing products or variants. "
            "Clean or migrate those values before changing its key, type, level or options."
        )
    next_required = attrs.get("required", instance.required)
    next_level = attrs.get("level", instance.level)
    if not instance.required and next_required and attribute_scope_has_records(instance.section, next_level):
        errors["required"] = (
            "Existing catalog records may not contain this attribute. "
            "Populate them before making it required."
        )
    if errors:
        raise serializers.ValidationError(errors)


class RetailTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RetailType
        fields = ["id", "key", "label", "description", "icon", "config", "sort_order"]
        read_only_fields = ["id"]


class AttributeDefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDef
        fields = ["id", "key", "label", "type", "level", "options", "required", "sort_order"]


def _validate_section_label_translations(value):
    if not isinstance(value, dict) or set(value) - {"en", "fr", "ar"}:
        raise serializers.ValidationError("Use only EN, FR and AR section names.")
    normalized = {}
    for language in ("en", "fr", "ar"):
        text = value.get(language, "")
        if not isinstance(text, str):
            raise serializers.ValidationError("Every translated section name must be text.")
        normalized[language] = text.strip()
        if len(normalized[language]) > 100:
            raise serializers.ValidationError("Section names are limited to 100 characters.")
    if not any(normalized.values()):
        raise serializers.ValidationError("Add a section name in at least one language.")
    return normalized


def _apply_section_label(attrs, instance):
    if "label_translations" in attrs:
        translations = attrs["label_translations"]
        attrs["label"] = translations.get("en") or next(value for value in translations.values() if value)
    elif "label" in attrs:
        translations = dict(getattr(instance, "label_translations", {}) or {})
        translations["en"] = attrs["label"].strip()
        attrs["label_translations"] = {
            language: str(translations.get(language, "")).strip()
            for language in ("en", "fr", "ar")
        }
    elif instance is None:
        raise serializers.ValidationError({"label_translations": "Add a section name in at least one language."})
    return attrs


def _localize_section_representation(data, request):
    translations = {
        language: str(value).strip()
        for language, value in (data.get("label_translations") or {}).items()
        if language in {"en", "fr", "ar"} and str(value).strip()
    }
    requested = (request.headers.get("Accept-Language", "en") if request else "en").split(",", 1)[0].split("-", 1)[0].lower()
    data["label"] = translations.get(requested) or translations.get("en") or next(iter(translations.values()), data["label"])
    data["label_translations"] = {
        language: translations.get(language, "")
        for language in ("en", "fr", "ar")
    }
    return data


class CatalogSectionSerializer(serializers.ModelSerializer):
    attribute_defs = AttributeDefSerializer(many=True, read_only=True)
    thumbnail = serializers.SerializerMethodField()

    class Meta:
        model = CatalogSection
        fields = [
            "id", "key", "label", "label_translations", "description", "thumbnail", "is_visible",
            "parent", "sort_mode", "icon", "sort_order", "attribute_defs",
            "seo_title", "seo_description", "seo_social_image",
        ]
        extra_kwargs = {"label": {"required": False}}

    def get_thumbnail(self, obj):
        return _asset_url(obj.thumbnail, self.context)

    def validate_label_translations(self, value):
        return _validate_section_label_translations(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return _apply_section_label(attrs, self.instance)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        return _localize_section_representation(data, self.context.get("request"))

    def validate_seo_title(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO title")

    def validate_seo_description(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO description")


class ProductVendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVendor
        fields = ["id", "name", "slug"]


class MerchantProductVendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVendor
        fields = ["id", "name", "slug", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        extra_kwargs = {"slug": {"required": False, "allow_blank": True}}

    def validate_slug(self, value):
        store = self.context.get("store")
        duplicate = ProductVendor.objects.filter(store=store, slug=value)
        if self.instance is not None:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if value and duplicate.exists():
            raise serializers.ValidationError("This vendor URL is already used in this store.")
        return value


class ProductReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = [
            "id", "customer_name", "rating", "title", "content",
            "verified_purchase", "created_at",
        ]
        read_only_fields = ["id", "verified_purchase", "created_at"]


class MerchantProductReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductReview
        fields = [
            "id", "product", "customer_name", "customer_email", "rating", "title",
            "content", "status", "verified_purchase", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "product", "customer_name", "customer_email", "rating", "title",
            "content", "verified_purchase", "created_at", "updated_at",
        ]


class ProductImageWriteSerializer(serializers.ModelSerializer):
    direct_upload = serializers.JSONField(write_only=True, required=False)

    class Meta:
        model = ProductImage
        fields = ["id", "image", "direct_upload", "media_type", "external_url", "alt_text", "is_primary", "order"]
        read_only_fields = ["id"]
        extra_kwargs = {"image": {"required": False}}

    def validate_image(self, f):
        return _validate_raster_image(f)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        media_type = attrs.get("media_type", ProductImage.MediaType.IMAGE)
        direct_upload = attrs.get("direct_upload")
        if media_type == ProductImage.MediaType.IMAGE and not attrs.get("image") and not direct_upload and not attrs.get("external_url"):
            raise serializers.ValidationError({"image": "Upload an image."})
        if media_type == ProductImage.MediaType.EXTERNAL_VIDEO and not attrs.get("external_url"):
            raise serializers.ValidationError({"external_url": "Provide an external video URL."})
        if attrs.get("is_primary") and media_type != ProductImage.MediaType.IMAGE:
            raise serializers.ValidationError({"is_primary": "Only an image can be the product cover."})
        return attrs

    def create(self, validated_data):
        direct_upload = validated_data.pop("direct_upload", None)
        if direct_upload:
            store = self.context["store"]
            product = self.context["product"]
            validated_data["image"] = validate_cloudinary_upload(
                direct_upload,
                folder=image_folder(store.id, "products", product.id),
            )
        return super().create(validated_data)


class ProductImageReadSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "image", "media_type", "external_url", "alt_text", "is_primary", "order"]

    def get_image(self, obj):
        return _asset_url(obj.image, self.context) or (
            obj.external_url if obj.media_type == ProductImage.MediaType.IMAGE else ""
        )


class StockVariantSerializer(serializers.ModelSerializer):
    available_quantity = serializers.IntegerField(read_only=True)
    image = ProductImageReadSerializer(read_only=True)
    effective_price = serializers.SerializerMethodField()
    effective_compare_at_price = serializers.SerializerMethodField()
    selections = serializers.SerializerMethodField()

    class Meta:
        model = StockVariant
        fields = [
            "id", "attributes", "price", "compare_at_price", "sku", "barcode", "weight", "image",
            "quantity", "reserved", "available_quantity", "extra", "created_at", "updated_at",
            "is_active", "effective_price", "effective_compare_at_price", "selections",
        ]
        read_only_fields = ["extra"]

    def validate(self, attrs):
        product = self.context.get("product")
        store = self.context.get("store")
        if product is None or store is None:
            raise serializers.ValidationError("Missing product/store context.")
        validate_attributes(attrs.get("attributes", {}), store, product.section_id, level="variant")
        return attrs

    def get_effective_price(self, obj):
        from .variant_services import effective_variant_pricing
        return effective_variant_pricing(obj.product, obj)["selling_price"]

    def get_effective_compare_at_price(self, obj):
        from .variant_services import effective_variant_pricing
        return effective_variant_pricing(obj.product, obj)["compare_at_price"]

    def get_selections(self, obj):
        return [{
            "option_id": selection.option_id,
            "option_key": selection.option.code,
            "option_label": selection.option.label,
            "value_id": selection.value_id,
            "value": selection.value.code,
            "value_label": selection.value.label,
        } for selection in obj.selections.select_related("option", "value").all()]


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageReadSerializer(many=True, read_only=True)
    variants = StockVariantSerializer(many=True, read_only=True)
    categories = CatalogSectionSerializer(many=True, read_only=True)
    vendors = ProductVendorSerializer(many=True, read_only=True)
    related_product_ids = serializers.PrimaryKeyRelatedField(source="related_products", many=True, read_only=True)
    inventory = serializers.SerializerMethodField()
    variants_count = serializers.IntegerField(source="variants.count", read_only=True)
    has_variants = serializers.SerializerMethodField()
    you_save_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "section", "categories", "name", "slug", "ref", "description",
            "price", "compare_at_price", "promo",
            "loyalty_points", "rating", "reviews_count",
            "extra", "attributes", "variant_options", "tags", "seo_title", "seo_description", "seo_social_image", "vendors",
            "related_product_ids", "uses_variants", "show_related_products",
            "is_active", "track_inventory", "is_featured",
            "merchandising_order", "inventory", "has_variants", "variants_count",
            "you_save_amount", "images", "variants", "created_at", "updated_at",
        ]
        read_only_fields = ["store", "created_at", "updated_at"]

    def get_inventory(self, obj):
        return obj.inventory

    def get_has_variants(self, obj):
        return obj.uses_variants and any(bool(v.attributes) for v in obj.variants.all())

    def validate(self, attrs):
        store = self.context.get("store")
        section = attrs.get("section") or getattr(self.instance, "section", None)
        if store is None or section is None:
            raise serializers.ValidationError("Missing store/section context.")
        if section.store_id != store.id:
            raise serializers.ValidationError("Section does not belong to this store.")
        validate_attributes(attrs.get("attributes", {}), store, section.id, level="product")
        return attrs

    def validate_seo_title(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO title")

    def validate_seo_description(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO description")


# ── Merchant (dashboard) serializers ──────────────────────────────────────────

class MerchantAttributeDefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttributeDef
        fields = ["id", "key", "label", "type", "level", "options", "required", "sort_order"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        if self.instance is not None:
            _validate_attribute_definition_change(self.instance, attrs)
        return attrs

    def create(self, validated_data):
        definition = AttributeDef(**validated_data)
        definition.full_clean()
        definition.save()
        return definition

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.full_clean()
        instance.save()
        return instance


class MerchantRetailTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RetailType
        fields = ["id", "key", "label", "description", "icon", "config", "sort_order"]
        read_only_fields = ["id"]


class MerchantSectionSerializer(serializers.ModelSerializer):
    attribute_defs = MerchantAttributeDefSerializer(many=True, required=False)

    class Meta:
        model = CatalogSection
        fields = [
            "id", "key", "label", "label_translations", "description", "thumbnail", "is_visible",
            "parent", "sort_mode", "icon", "sort_order", "attribute_defs",
            "seo_title", "seo_description", "seo_social_image",
        ]
        read_only_fields = ["id"]
        extra_kwargs = {"label": {"required": False}}

    def validate_label_translations(self, value):
        return _validate_section_label_translations(value)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return _apply_section_label(attrs, self.instance)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        return _localize_section_representation(data, self.context.get("request"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store is not None:
            self.fields["parent"].queryset = CatalogSection.objects.filter(store=store)

    def validate_parent(self, parent):
        store = self.context.get("store")
        if parent and store and parent.store_id != store.id:
            raise serializers.ValidationError("Parent category must belong to this store.")
        if self.instance and parent and parent.pk == self.instance.pk:
            raise serializers.ValidationError("A category cannot be its own parent.")
        return parent

    def validate_key(self, value):
        from .models import CatalogSectionSlugRedirect
        store = self.context.get("store")
        if store and CatalogSectionSlugRedirect.objects.filter(store=store, old_slug=value).exists():
            raise serializers.ValidationError("This collection address is reserved by its redirect history.")
        return value

    def validate_seo_title(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO title")

    def validate_seo_description(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO description")

    def validate_thumbnail(self, value):
        if not value:
            return value
        return _validate_raster_image(value)

    def validate_attribute_defs(self, definitions):
        keys = [definition["key"] for definition in definitions]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("Attribute keys must be unique within a section.")
        if self.instance is None:
            return definitions
        existing = {definition.key: definition for definition in self.instance.attribute_defs.all()}
        incoming = {definition["key"]: definition for definition in definitions}
        errors = {}
        for key, definition in existing.items():
            if key not in incoming:
                if attribute_key_in_use(self.instance, key):
                    errors[key] = "This attribute is still used by catalog records."
                continue
            try:
                _validate_attribute_definition_change(definition, incoming[key])
            except serializers.ValidationError as exc:
                errors[key] = exc.detail
        if errors:
            raise serializers.ValidationError(errors)
        return definitions

    @transaction.atomic
    def create(self, validated_data):
        defs = validated_data.pop("attribute_defs", [])
        store = validated_data.get("store")
        section = CatalogSection(**validated_data)
        section.full_clean()
        section.save()
        for d in defs:
            definition = AttributeDef(store=store, section=section, **d)
            definition.full_clean()
            definition.save()
        return section

    @transaction.atomic
    def update(self, instance, validated_data):
        defs = validated_data.pop("attribute_defs", None)
        store = validated_data.get("store", instance.store)
        old_key = instance.key
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.full_clean()
        instance.save()
        if old_key != instance.key:
            from .models import CatalogSectionSlugRedirect
            CatalogSectionSlugRedirect.objects.create(store=store, section=instance, old_slug=old_key)
        if defs is not None:
            existing = {definition.key: definition for definition in instance.attribute_defs.all()}
            retained = set()
            for d in defs:
                key = d["key"]
                retained.add(key)
                definition = existing.get(key)
                if definition is None:
                    definition = AttributeDef(store=store, section=instance)
                for field, value in d.items():
                    setattr(definition, field, value)
                definition.full_clean()
                definition.save()
            for key, definition in existing.items():
                if key not in retained:
                    definition.delete()
        return instance


class MerchantProductSerializer(serializers.ModelSerializer):
    images = ProductImageReadSerializer(many=True, read_only=True)
    variants = StockVariantSerializer(many=True, read_only=True)
    inventory_ready = serializers.SerializerMethodField()
    inventory = serializers.SerializerMethodField()
    variants_count = serializers.IntegerField(source="variants.count", read_only=True)
    has_variants = serializers.SerializerMethodField()
    you_save_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    categories = CatalogSectionSerializer(many=True, read_only=True)
    category_ids = serializers.PrimaryKeyRelatedField(
        source="categories", many=True, queryset=CatalogSection.objects.none(), required=False,
    )
    vendors = ProductVendorSerializer(many=True, read_only=True)
    vendor_ids = serializers.PrimaryKeyRelatedField(
        source="vendors", many=True, queryset=ProductVendor.objects.none(), required=False,
    )
    related_product_ids = serializers.PrimaryKeyRelatedField(
        source="related_products", many=True, queryset=Product.objects.none(), required=False,
    )
    clear_variants = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Product
        fields = [
            "id", "section", "categories", "category_ids", "name", "slug", "ref",
            "description", "price", "compare_at_price", "cost_price", "promo",
            "loyalty_points", "rating", "reviews_count",
            "extra", "attributes", "variant_options", "tags", "seo_title", "seo_description", "seo_social_image", "vendors",
            "vendor_ids", "related_product_ids", "uses_variants", "clear_variants",
            "show_related_products", "is_active", "track_inventory",
            "is_featured", "merchandising_order", "inventory", "has_variants",
            "variants_count", "you_save_amount", "images", "variants", "inventory_ready",
            "created_at", "updated_at",
        ]
        read_only_fields = ["store", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store is not None:
            self.fields["category_ids"].child_relation.queryset = CatalogSection.objects.filter(store=store)
            self.fields["vendor_ids"].child_relation.queryset = ProductVendor.objects.filter(store=store)
            products = Product.objects.filter(store=store)
            instance_pk = getattr(self.instance, "pk", None)
            if instance_pk is not None:
                products = products.exclude(pk=instance_pk)
            self.fields["related_product_ids"].child_relation.queryset = products

    def validate(self, attrs):
        store = self.context.get("store")
        request = self.context.get("request")
        if request is not None and store is not None:
            field_map = {
                "name": "name", "description": "description", "price": "price",
                "compare_at_price": "compare_at_price", "cost_price": "cost_price",
                "category_ids": "categories", "section": "categories", "images": "images",
                "inventory": "inventory", "track_inventory": "inventory",
                "seo_title": "seo", "seo_description": "seo", "ref": "sku", "extra": "sku",
            }
            controlled = {field_map[key] for key in attrs if key in field_map}
            if controlled:
                require_fields(request.user, store, "products", controlled, "edit")
        section = attrs.get("section") or getattr(self.instance, "section", None)
        if store is None or section is None:
            raise serializers.ValidationError("Missing store/section context.")
        if section.store_id != store.id:
            raise serializers.ValidationError("Section does not belong to this store.")
        reference = attrs.get("ref", getattr(self.instance, "ref", ""))
        duplicate_reference = Product.objects.filter(store=store, ref=reference)
        if self.instance is not None:
            duplicate_reference = duplicate_reference.exclude(pk=self.instance.pk)
        if duplicate_reference.exists():
            raise serializers.ValidationError({"ref": "This reference is already used in this store."})
        slug = attrs.get("slug", getattr(self.instance, "slug", ""))
        if slug:
            duplicate_slug = Product.objects.filter(store=store, slug=slug)
            if self.instance is not None:
                duplicate_slug = duplicate_slug.exclude(pk=self.instance.pk)
            if duplicate_slug.exists():
                raise serializers.ValidationError({"slug": "This product URL is already used in this store."})
            from .models import ProductSlugRedirect
            if ProductSlugRedirect.objects.filter(store=store, old_slug=slug).exists():
                raise serializers.ValidationError({"slug": "This product URL is reserved by its redirect history."})
        categories = attrs.get("categories")
        if categories is not None and any(category.store_id != store.id for category in categories):
            raise serializers.ValidationError({"category_ids": "Every category must belong to this store."})
        vendors = attrs.get("vendors")
        if vendors is not None and any(vendor.store_id != store.id for vendor in vendors):
            raise serializers.ValidationError({"vendor_ids": "Every vendor must belong to this store."})
        related = attrs.get("related_products")
        if related is not None and any(product.store_id != store.id for product in related):
            raise serializers.ValidationError({"related_product_ids": "Every related product must belong to this store."})
        if "description" in attrs:
            attrs["description"] = sanitize_product_description(attrs["description"])
        existing_variants = list(self.instance.variants.all()) if self.instance is not None else []
        has_only_default_inventory = (
            len(existing_variants) == 1 and is_default_inventory_variant(existing_variants[0])
        )
        if self.instance is not None and section.id != self.instance.section_id:
            if existing_variants and not has_only_default_inventory:
                raise serializers.ValidationError({
                    "section": "Remove existing variants before changing the product section."
                })
        if "attributes" not in attrs and self.instance is not None:
            attrs["attributes"] = self.instance.attributes
        previous_sizing = (self.instance.attributes or {}).get("size_system") if self.instance is not None else None
        attrs["attributes"] = validate_attributes(
            attrs.get("attributes", {}), store, section.id, level="product",
        )
        variant_options = attrs.get(
            "variant_options",
            self.instance.variant_options if self.instance is not None else [],
        )
        attrs["variant_options"] = validate_product_variant_options(variant_options)
        next_sizing = attrs["attributes"].get("size_system")
        if self.instance is not None and existing_variants and not has_only_default_inventory and previous_sizing != next_sizing:
            raise serializers.ValidationError({
                "attributes": {"size_system": "Remove existing variants before changing the sizing system."}
            })
        extra = attrs.get("extra", self.instance.extra if self.instance is not None else {})
        attrs["extra"] = validate_extra_fields(extra, store.retail_type, level="product", config=store.catalog_config.get("product_fields", {}))
        _validate_store_identifiers(store, attrs["extra"], self.instance)
        tags = attrs.get("tags", getattr(self.instance, "tags", []))
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            raise serializers.ValidationError({"tags": "Enter tags as a list of non-empty values."})
        attrs["tags"] = list(dict.fromkeys(tag.strip() for tag in tags))
        selling_price = attrs.get("price", getattr(self.instance, "price", None))
        compare_at = attrs.get("compare_at_price", getattr(self.instance, "compare_at_price", None))
        if compare_at is not None and selling_price is not None and compare_at <= selling_price:
            raise serializers.ValidationError({"compare_at_price": "Must be greater than the selling price."})
        next_uses_variants = attrs.get(
            "uses_variants",
            self.instance.uses_variants if self.instance is not None else False,
        )
        if not next_uses_variants:
            attrs["variant_options"] = []
        clear_variants = attrs.get("clear_variants", False)
        configured_variants = [variant for variant in existing_variants if variant.attributes]
        variant_options_changed = (
            self.instance is not None
            and attrs["variant_options"] != self.instance.variant_options
        )
        # Confirmed conversion removes the old combinations below; validating
        # them against an empty option schema would prevent every conversion.
        # The confirmation and reserved-stock guards must still run.
        if configured_variants and variant_options_changed and not (clear_variants and not next_uses_variants):
            for variant in configured_variants:
                try:
                    if attrs["variant_options"]:
                        validate_product_variant_attributes(variant.attributes, attrs["variant_options"])
                    else:
                        validate_attributes(variant.attributes, store, section.id, level="variant")
                except Exception as exc:
                    raise serializers.ValidationError({
                        "variant_options": "Existing combinations do not match these choices. Remove them before changing the choices."
                    }) from exc
        if (
            self.instance is not None
            and not self.instance.uses_variants
            and next_uses_variants
            and any(variant.reserved for variant in existing_variants)
        ):
            raise serializers.ValidationError({
                "uses_variants": "Product choices cannot be enabled while simple-product stock is reserved. Complete or cancel the open orders first."
            })
        if self.instance is not None and not next_uses_variants and configured_variants:
            if not clear_variants:
                raise serializers.ValidationError({
                    "uses_variants": "Confirm removal of the existing option combinations first."
                })
            if any(variant.reserved for variant in existing_variants):
                raise serializers.ValidationError({
                    "uses_variants": "Options with reserved stock cannot be removed. Complete or cancel the open orders first."
                })
        will_be_active = attrs.get("is_active", self.instance.is_active if self.instance is not None else True)
        if self.instance is not None and will_be_active and next_uses_variants and not configured_variants:
            raise serializers.ValidationError({
                "is_active": "Add at least one option combination before activating this product."
            })
        return attrs

    def validate_seo_title(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO title")

    def validate_seo_description(self, value):
        from store.seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO description")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        store = self.context.get("store")
        membership = membership_for(request.user, store) if request is not None and store is not None else None
        if membership is None:
            return data
        rules = effective_fields(membership).get("products", {})
        groups = {
            "name": ["name"], "description": ["description"], "images": ["images"],
            "categories": ["section", "categories", "category_ids"],
            "price": ["price"], "compare_at_price": ["compare_at_price", "promo", "you_save_amount"],
            "cost_price": ["cost_price"], "inventory": ["inventory", "variants", "inventory_ready"],
            "sku": ["ref", "extra"], "seo": ["seo_title", "seo_description", "seo_social_image"],
        }
        for field, response_fields in groups.items():
            if not rules.get(field, {}).get("view", False):
                for response_field in response_fields:
                    data.pop(response_field, None)
        return data

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("clear_variants", None)
        product = super().create(validated_data)
        product.categories.add(product.section)
        ensure_product_inventory_identity(product)
        _sync_product_option_records(product)
        product.refresh_from_db(fields=["variant_options"])
        _sync_simple_variant_identity(product)
        return product

    @transaction.atomic
    def update(self, instance, validated_data):
        clear_variants = validated_data.pop("clear_variants", False)
        previous_section_id = instance.section_id
        previous_slug = instance.slug
        product = super().update(instance, validated_data)
        product.categories.add(product.section)
        if clear_variants and not product.uses_variants:
            product.variants.all().delete()
            ensure_product_inventory_identity(product)
        if product.section_id != previous_section_id:
            variants = list(product.variants.select_for_update())
            if len(variants) == 1 and is_default_inventory_variant(variants[0]):
                variants[0].delete()
                ensure_product_inventory_identity(product)
        _sync_product_option_records(product)
        product.refresh_from_db(fields=["variant_options"])
        _sync_simple_variant_identity(product)
        if previous_slug and previous_slug != product.slug:
            from .models import ProductSlugRedirect
            ProductSlugRedirect.objects.create(store=product.store, product=product, old_slug=previous_slug)
        return product

    def get_inventory_ready(self, obj):
        return obj.variants.exists()

    def get_inventory(self, obj):
        return obj.inventory

    def get_has_variants(self, obj):
        return obj.uses_variants and any(bool(variant.attributes) for variant in obj.variants.all())


class MerchantVariantSerializer(serializers.ModelSerializer):
    available_quantity = serializers.IntegerField(read_only=True)
    image = ProductImageReadSerializer(read_only=True)
    image_id = serializers.PrimaryKeyRelatedField(
        source="image", queryset=ProductImage.objects.none(), required=False, allow_null=True,
    )
    effective_price = serializers.SerializerMethodField()
    effective_compare_at_price = serializers.SerializerMethodField()
    effective_cost_price = serializers.SerializerMethodField()

    class Meta:
        model = StockVariant
        fields = [
            "id", "attributes", "price", "compare_at_price", "cost_price", "sku", "barcode", "weight", "image", "image_id",
            "quantity", "reserved", "available_quantity", "extra", "created_at", "updated_at",
            "is_active", "archived_at", "effective_price", "effective_compare_at_price", "effective_cost_price",
        ]
        read_only_fields = ["id", "reserved", "archived_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        product = self.context.get("product")
        if product is not None:
            self.fields["image_id"].queryset = ProductImage.objects.filter(product=product)

    def validate(self, attrs):
        product = self.context.get("product")
        store = self.context.get("store")
        if product is None or store is None:
            raise serializers.ValidationError("Missing product/store context.")
        if "attributes" not in attrs and self.instance is not None:
            attrs["attributes"] = self.instance.attributes
        if (
            self.instance is not None
            and is_default_inventory_variant(self.instance)
            and attrs.get("attributes") == {}
        ):
            # Updating simple-product stock must not require category options.
            pass
        elif product.variant_options:
            attrs["attributes"] = validate_product_variant_attributes(
                attrs.get("attributes", {}), product.variant_options,
            )
        else:
            attrs["attributes"] = validate_attributes(
                attrs.get("attributes", {}), store, product.section_id, level="variant",
            )
        attrs["attributes"] = validate_variant_sizing(attrs["attributes"], product)
        duplicate = StockVariant.objects.filter(product=product, attributes=attrs["attributes"], is_active=True)
        if self.instance is not None:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError({
                "attributes": "A variant with this option combination already exists."
            })
        extra = attrs.get("extra", self.instance.extra if self.instance is not None else {})
        attrs["extra"] = validate_extra_fields(extra, store.retail_type, level="variant", config=store.catalog_config.get("variant_fields", {}))
        for key in ("sku", "barcode"):
            if key not in attrs and extra.get(key):
                attrs[key] = str(extra[key]).strip()
        if "weight" not in attrs and extra.get("weight") not in (None, ""):
            attrs["weight"] = extra["weight"]
        _validate_store_identifiers(store, attrs["extra"], self.instance)
        _validate_variant_identifiers(store, attrs, self.instance)
        quantity = attrs.get("quantity", self.instance.quantity if self.instance is not None else 0)
        reserved = self.instance.reserved if self.instance is not None else 0
        if quantity < reserved:
            raise serializers.ValidationError({
                "quantity": f"Cannot be lower than the {reserved} units currently reserved."
            })
        price = attrs.get("price", self.instance.price if self.instance is not None else None)
        compare_at = attrs.get("compare_at_price", self.instance.compare_at_price if self.instance is not None else None)
        effective_price = price if price is not None else product.price
        effective_compare = compare_at if compare_at is not None else product.compare_at_price
        if effective_compare is not None and effective_compare <= effective_price:
            raise serializers.ValidationError({"compare_at_price": "Must exceed the effective selling price."})
        return attrs

    def _pricing(self, obj):
        from .variant_services import effective_variant_pricing
        return effective_variant_pricing(obj.product, obj)

    def get_effective_price(self, obj): return self._pricing(obj)["selling_price"]
    def get_effective_compare_at_price(self, obj): return self._pricing(obj)["compare_at_price"]
    def get_effective_cost_price(self, obj): return self._pricing(obj)["cost_price"]

    @transaction.atomic
    def create(self, validated_data):
        product = validated_data["product"]
        archived = product.variants.select_for_update().filter(
            attributes=validated_data.get("attributes", {}), is_active=False,
        ).first()
        if archived is not None:
            for field, value in validated_data.items(): setattr(archived, field, value)
            archived.is_active, archived.archived_at = True, None
            archived.full_clean(); archived.save()
            return _sync_variant_selection_records(archived)
        if not product.uses_variants:
            if product.variants.filter(reserved__gt=0).exists():
                raise serializers.ValidationError({
                    "product": "This product has reserved stock. Complete or cancel the open orders before adding choices."
                })
            # Rolling-client compatibility: creating a real option combination
            # is itself an explicit request to turn this product into a
            # configurable one. The current dashboard sends the switch first.
            Product.objects.filter(pk=product.pk).update(uses_variants=True)
            product.uses_variants = True
        variants = product.variants.select_for_update()
        placeholder = next((variant for variant in variants if variant.attributes == {}), None)
        if placeholder is not None and variants.count() == 1:
            if placeholder.reserved:
                raise serializers.ValidationError({
                    "product": "This product has reserved stock. Complete or cancel the open orders before adding choices."
                })
            for field, value in validated_data.items():
                setattr(placeholder, field, value)
            placeholder.full_clean()
            placeholder.save()
            return _sync_variant_selection_records(placeholder)
        return _sync_variant_selection_records(super().create(validated_data))

    @transaction.atomic
    def update(self, instance, validated_data):
        return _sync_variant_selection_records(super().update(instance, validated_data))
