import re
from copy import deepcopy

from rest_framework import serializers

from django.core.exceptions import ValidationError as DjangoValidationError

from common.media_uploads import (
    STOREFRONT_IMAGE_UPLOAD,
    image_folder,
    media_asset_url,
    validate_cloudinary_upload,
)

from .models import (
    HEX_COLOR_RE,
    Store,
    StoreConfig,
    StoreCustomer,
    StorefrontAsset,
    StorefrontDomain,
    StorefrontPage,
    StoreMember,
    validate_storefront_sections,
    validate_storefront_page_sections,
    default_content_sections,
    localized_text,
    normalize_storefront_appearance,
)


STORE_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
STORE_LOGO_TYPES = {"image/jpeg", "image/png", "image/webp"}
STORE_LOGO_MAX_BYTES = 5 * 1024 * 1024


class StorefrontDomainSerializer(serializers.ModelSerializer):
    healthy = serializers.BooleanField(read_only=True)
    url = serializers.SerializerMethodField()
    redirect_to = serializers.SerializerMethodField()

    class Meta:
        model = StorefrontDomain
        fields = ["id", "hostname", "display_hostname", "kind", "status", "is_primary",
                  "healthy", "url", "ownership_verified_at", "expected_dns_records",
                  "provider_verified", "certificate_status", "health_verified_at",
                  "last_checked_at", "next_retry_at", "failure_code", "public_message",
                  "redirect_to", "created_at", "updated_at"]
        read_only_fields = fields

    def get_url(self, instance):
        return f"https://{instance.hostname}"

    def get_redirect_to(self, instance):
        from .domain_services import domain_redirect_target
        return domain_redirect_target(instance)


class StoreLogoUploadSerializer(serializers.Serializer):
    logo = serializers.ImageField()

    def validate_logo(self, value):
        if getattr(value, "content_type", "") not in STORE_LOGO_TYPES:
            raise serializers.ValidationError("Choose a PNG, JPEG or WebP image.")
        if value.size > STORE_LOGO_MAX_BYTES:
            raise serializers.ValidationError("The logo must be smaller than 5 MB.")
        return value


class StorefrontAssetSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField(read_only=True)
    direct_upload = serializers.JSONField(write_only=True, required=False)

    class Meta:
        model = StorefrontAsset
        fields = ["id", "image", "direct_upload", "image_url", "created_at"]
        read_only_fields = ["id", "image_url", "created_at"]
        extra_kwargs = {"image": {"write_only": True, "required": False}}

    def validate_image(self, value):
        if getattr(value, "content_type", "") not in STORE_LOGO_TYPES:
            raise serializers.ValidationError("Choose a PNG, JPEG or WebP image.")
        if value.size > 8 * 1024 * 1024:
            raise serializers.ValidationError("The image must be smaller than 8 MB.")
        return value

    def get_image_url(self, instance):
        return media_asset_url(instance.image, request=self.context.get("request"))

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("image") and not attrs.get("direct_upload"):
            raise serializers.ValidationError({"image": "Upload an image."})
        return attrs

    def create(self, validated_data):
        direct_upload = validated_data.pop("direct_upload", None)
        if direct_upload:
            store = self.context["store"]
            validated_data["image"] = validate_cloudinary_upload(
                direct_upload,
                folder=image_folder(store.id, "storefront"),
                profile=STOREFRONT_IMAGE_UPLOAD,
            )
        return super().create(validated_data)


class StoreConfigSerializer(serializers.ModelSerializer):
    payment_gateway = serializers.SerializerMethodField()
    can_remove_platform_branding = serializers.SerializerMethodField()

    class Meta:
        model = StoreConfig
        fields = [
            "plan", "currency", "default_locale", "payment_provider", "payment_gateway", "payment_keys", "cod_enabled", "from_email", "brand_color",
            "loyalty_enabled", "shipping_fee", "free_shipping_enabled", "free_shipping_threshold", "free_shipping_currency",
            "invoice_enabled", "invoice_legal_name", "invoice_billing_address", "invoice_country", "invoice_contact_email",
            "invoice_identifiers", "invoice_issuer_scope", "invoice_series_prefix", "invoice_tax_regime",
            "invoice_tax_rate", "invoice_prices_include_tax", "invoice_shipping_tax_rate", "invoice_tax_wording",
            "design", "contact_email", "support_phone", "whatsapp_phone",
            "social_links", "homepage_product_source", "homepage_product_limit", "domain",
            "appearance", "remove_platform_branding", "can_remove_platform_branding",
            "seo_title", "seo_description", "seo_social_image", "allow_search_indexing",
        ]
        # Domain publication has a dedicated ownership/DNS/HTTPS workflow.
        # Keeping the legacy value readable avoids a breaking response change,
        # but generic settings writes must never make an unverified host live.
        read_only_fields = ["plan", "payment_gateway", "domain", "can_remove_platform_branding"]

    def get_can_remove_platform_branding(self, instance):
        from subscriptions.entitlements import owner_has_store_entitlement
        from subscriptions.policies import REMOVE_BRANDING_KEY

        return owner_has_store_entitlement(instance.store, REMOVE_BRANDING_KEY)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        from django.conf import settings
        future = getattr(settings, "STOREFRONT_FUTURE_PAYMENT_PROVIDERS", {})
        if instance.payment_provider in future:
            data["payment_keys"] = {}
        elif self.context.get("can_manage_payment_keys", False):
            from django.core.exceptions import ImproperlyConfigured
            from payments.gateways import get_gateway
            try:
                data["payment_keys"] = dict(get_gateway(instance.payment_provider).store_credentials(instance))
            except ImproperlyConfigured:
                data["payment_keys"] = {}
        else:
            data["payment_keys"] = {}
        return data

    def update(self, instance, validated_data):
        homepage_changed = bool({"homepage_product_source", "homepage_product_limit"} & set(validated_data))
        submitted_credentials = validated_data.pop("payment_keys", None)
        provider = validated_data.get("payment_provider", instance.payment_provider)
        if submitted_credentials is not None:
            from payments.gateways.registry import configured_backends
            existing = instance.payment_keys or {}
            is_vault = isinstance(existing, dict) and any(
                key in configured_backends() and isinstance(value, dict)
                for key, value in existing.items()
            )
            vault = dict(existing) if is_vault else {instance.payment_provider: dict(existing)}
            # Selecting a previously configured provider with an empty form
            # means “switch back”, not “erase its stored credentials”. To
            # intentionally clear it, save the empty form once it is selected.
            if submitted_credentials or provider == instance.payment_provider or provider not in vault:
                vault[provider] = submitted_credentials
            validated_data["payment_keys"] = vault
        instance = super().update(instance, validated_data)
        if homepage_changed:
            from .models import sync_homepage_merchandising

            sync_homepage_merchandising(instance, publish=True)
        return instance

    def validate_remove_platform_branding(self, value):
        if not value:
            return value
        from subscriptions.entitlements import owner_has_store_entitlement
        from subscriptions.policies import REMOVE_BRANDING_KEY

        if not owner_has_store_entitlement(self.context["store"], REMOVE_BRANDING_KEY):
            raise serializers.ValidationError("This option is not included in the store owner's current plan.")
        return value

    def validate_seo_title(self, value):
        from .seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO title")

    def validate_seo_description(self, value):
        from .seo_validation import clean_seo_text
        return clean_seo_text(value, label="SEO description")

    def get_payment_gateway(self, instance):
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from payments.gateways.registry import configured_backends, get_gateway

        future = dict(getattr(settings, "STOREFRONT_FUTURE_PAYMENT_PROVIDERS", {}))
        available = [
            {
                "code": code,
                "name": get_gateway(code).display_name,
                "credential_fields": get_gateway(code).credential_fields(),
                "availability": "available",
                "selectable": True,
            }
            for code in configured_backends()
            if code not in future
        ]
        available.extend(
            {
                "code": code,
                "name": name,
                "credential_fields": [],
                "availability": "future",
                "selectable": False,
            }
            for code, name in future.items()
        )
        try:
            selected = get_gateway(instance.payment_provider)
        except ImproperlyConfigured:
            return {
                "code": instance.payment_provider,
                "name": instance.payment_provider,
                "configured": False,
                "sandbox": False,
                "details": {},
                "credential_fields": [],
                "available_providers": available,
            }
        if instance.payment_provider in future:
            return {
                "code": instance.payment_provider,
                "name": future[instance.payment_provider],
                "configured": False,
                "sandbox": False,
                "details": {"availability": "future"},
                "credential_fields": [],
                "available_providers": available,
            }
        return {
            **selected.public_descriptor(selected.store_credentials(instance)),
            "available_providers": available,
        }

    def validate_payment_provider(self, value):
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from payments.gateways import get_gateway

        future = getattr(settings, "STOREFRONT_FUTURE_PAYMENT_PROVIDERS", {})
        if value in future and (self.instance is None or value != self.instance.payment_provider):
            raise serializers.ValidationError("This payment provider is planned for a future release.")
        try:
            get_gateway(value)
        except ImproperlyConfigured as exc:
            raise serializers.ValidationError("This payment provider is not available.") from exc
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        current = self.instance
        if attrs.get("invoice_enabled") is True and current and not current.invoice_enabled:
            from subscriptions.entitlements import owner_has_store_entitlement
            from subscriptions.policies import INVOICING_KEY

            if not owner_has_store_entitlement(self.context["store"], INVOICING_KEY):
                raise serializers.ValidationError({
                    "invoice_enabled": "Invoice issuance is not included in the store owner's current plan."
                })
        currency = attrs.get("currency", current.currency if current else "")
        free_enabled = attrs.get("free_shipping_enabled", current.free_shipping_enabled if current else False)
        threshold = attrs.get("free_shipping_threshold", current.free_shipping_threshold if current else None)
        threshold_currency = attrs.get("free_shipping_currency", current.free_shipping_currency if current else "")
        if free_enabled and threshold is None:
            raise serializers.ValidationError({"free_shipping_threshold": "Enter a free-shipping threshold."})
        if free_enabled and threshold_currency != currency:
            raise serializers.ValidationError({"free_shipping_currency": "Threshold currency must match the store currency."})
        identifiers = attrs.get("invoice_identifiers", current.invoice_identifiers if current else {})
        allowed_identifiers = {"ice", "if", "tp", "rc"}
        if not isinstance(identifiers, dict) or set(identifiers) - allowed_identifiers:
            raise serializers.ValidationError({"invoice_identifiers": "Use only ICE, IF, TP and RC identifiers."})
        if any(len(str(value)) > 80 for value in identifiers.values()):
            raise serializers.ValidationError({"invoice_identifiers": "Each identifier is limited to 80 characters."})
        enabled = attrs.get("invoice_enabled", current.invoice_enabled if current else False)
        if enabled:
            required = {
                "invoice_legal_name": attrs.get("invoice_legal_name", current.invoice_legal_name if current else ""),
                "invoice_billing_address": attrs.get("invoice_billing_address", current.invoice_billing_address if current else ""),
                "invoice_country": attrs.get("invoice_country", current.invoice_country if current else ""),
                "invoice_contact_email": attrs.get("invoice_contact_email", current.invoice_contact_email if current else ""),
                "invoice_issuer_scope": attrs.get("invoice_issuer_scope", current.invoice_issuer_scope if current else ""),
                "invoice_series_prefix": attrs.get("invoice_series_prefix", current.invoice_series_prefix if current else ""),
            }
            missing = {key: "This field is required before invoice issuance can be enabled." for key, value in required.items() if not str(value).strip()}
            tax_rate = attrs.get("invoice_tax_rate", current.invoice_tax_rate if current else None)
            if tax_rate is None:
                missing["invoice_tax_rate"] = "Enter the store tax percentage. Use 0 for tax-exempt products."
            if missing:
                raise serializers.ValidationError(missing)
        if "payment_keys" not in attrs:
            return attrs
        credentials = attrs["payment_keys"]
        if not isinstance(credentials, dict):
            raise serializers.ValidationError({"payment_keys": "Payment credentials must be an object."})
        provider = attrs.get("payment_provider", self.instance.payment_provider if self.instance else "")
        from django.conf import settings
        if provider in getattr(settings, "STOREFRONT_FUTURE_PAYMENT_PROVIDERS", {}):
            raise serializers.ValidationError({
                "payment_keys": "This payment provider is planned for a future release."
            })
        from payments.gateways import get_gateway
        fields = {item["key"]: item for item in get_gateway(provider).credential_fields()}
        unknown = set(credentials) - set(fields)
        if unknown:
            raise serializers.ValidationError({"payment_keys": "These payment credential fields are not supported."})
        if credentials:
            missing = [item["label"] for item in fields.values() if item.get("required") and not credentials.get(item["key"])]
            if missing:
                raise serializers.ValidationError({"payment_keys": "Complete all required payment credentials."})
            invalid_booleans = [
                item["label"] for item in fields.values()
                if item.get("type") == "boolean"
                and item["key"] in credentials
                and not isinstance(credentials[item["key"]], bool)
            ]
            if invalid_booleans:
                raise serializers.ValidationError({"payment_keys": "A payment option has an invalid value."})
        return attrs

    def validate_design(self, value):
        from .design_access import can_use_design, is_ready_design

        config = self.instance
        if config is not None:
            from subscriptions.services import sync_store_entitlement
            sync_store_entitlement(config.store)
            config.refresh_from_db(fields=["plan"])
        plan = config.plan if config is not None else StoreConfig.Plans.FREE
        if not is_ready_design(value):
            raise serializers.ValidationError(
                "Choose a storefront family that is ready for use."
            )
        if not can_use_design(plan, value):
            raise serializers.ValidationError(
                "This premium design requires an active Premium plan."
            )
        return value

    def validate_brand_color(self, value):
        import re

        from .models import HEX_COLOR_RE

        if not re.match(HEX_COLOR_RE, value):
            raise serializers.ValidationError("Enter a 6-digit hex color, e.g. #1F8A8C.")
        return value

    def validate_homepage_product_limit(self, value):
        if not 4 <= value <= 24:
            raise serializers.ValidationError("Choose between 4 and 24 products.")
        return value

    def validate_appearance(self, value):
        try:
            return normalize_storefront_appearance(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc



class StorefrontPageSerializer(serializers.ModelSerializer):
    """Merchant editor contract; only the draft can be changed directly."""

    is_protected = serializers.BooleanField(read_only=True)

    class Meta:
        model = StorefrontPage
        fields = [
            "page_key", "page_type", "route", "title", "is_visible", "show_in_navigation",
            "sort_order", "is_protected", "draft_sections", "published_sections",
            "revision", "published_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "page_key", "page_type", "route", "sort_order", "is_protected",
            "published_sections", "revision", "published_at", "created_at", "updated_at",
        ]

    def validate_draft_sections(self, value):
        try:
            if self.instance:
                validate_storefront_page_sections(self.instance.page_key, value)
            else:
                validate_storefront_sections(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def validate_title(self, value):
        from .models import _validate_localized_text

        try:
            _validate_localized_text(value, "Page title", required=True, max_length=120)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance and self.instance.is_protected and attrs.get("is_visible") is False:
            raise serializers.ValidationError({"is_visible": "This essential store page must remain available."})
        return attrs


class StorefrontPageCreateSerializer(serializers.Serializer):
    title = serializers.DictField(child=serializers.CharField(max_length=120, allow_blank=True))
    route = serializers.SlugField(max_length=80)
    show_in_navigation = serializers.BooleanField(default=True)

    def validate_title(self, value):
        from .models import _validate_localized_text

        try:
            _validate_localized_text(value, "Page title", required=True, max_length=120)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from exc
        return {language: value.get(language, "") for language in ("en", "fr", "ar")}

    def create(self, validated_data):
        store = self.context["store"]
        title = validated_data["title"]
        sections = default_content_sections(title, localized_text(), localized_text())
        next_order = (store.storefront_pages.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0) + 1
        return StorefrontPage.objects.create(
            store=store,
            page_key=validated_data["route"],
            route=validated_data["route"],
            title=title,
            page_type=StorefrontPage.PageTypes.CONTENT,
            is_visible=True,
            show_in_navigation=validated_data["show_in_navigation"],
            sort_order=next_order,
            draft_sections=sections,
            published_sections=deepcopy(sections),
        )


class CustomerSerializer(serializers.ModelSerializer):
    """A storefront customer + lightweight purchase summary for the dashboard."""
    email = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    city = serializers.SerializerMethodField()
    country = serializers.SerializerMethodField()
    orders_count = serializers.SerializerMethodField()
    total_spent = serializers.SerializerMethodField()
    address = serializers.CharField(source="profile.address", read_only=True)
    preferred_language = serializers.CharField(source="profile.preferred_language", read_only=True)
    email_verified = serializers.SerializerMethodField()
    phone_verified = serializers.BooleanField(source="profile.is_phone_verified", read_only=True)
    marketing_subscribed = serializers.SerializerMethodField()
    last_order_at = serializers.SerializerMethodField()

    class Meta:
        model = StoreCustomer
        fields = [
            "id", "email", "name", "phone", "city", "country", "address",
            "preferred_language", "email_verified", "phone_verified",
            "marketing_subscribed", "loyalty_points", "orders_count",
            "total_spent", "last_order_at", "created_at",
        ]

    def get_email(self, obj):
        return obj.profile.user.email

    def get_name(self, obj):
        user = obj.profile.user
        return f"{user.first_name} {user.last_name}".strip() or user.email

    def get_phone(self, obj):
        phone = obj.profile.phone
        return str(phone) if phone else ""

    def get_city(self, obj):
        return obj.profile.city

    def get_country(self, obj):
        return obj.profile.country

    def get_orders_count(self, obj):
        return self.context["orders_by_user"].get(obj.profile_id, 0)

    def get_total_spent(self, obj):
        return float(self.context["spent_by_user"].get(obj.profile_id, 0) or 0)

    def get_email_verified(self, obj):
        return obj.profile.user.email_verified_at is not None

    def get_marketing_subscribed(self, obj):
        return obj.profile.user.email.lower() in self.context.get("subscribed_emails", set())

    def get_last_order_at(self, obj):
        value = self.context.get("last_order_by_user", {}).get(obj.profile_id)
        return value.isoformat() if value else None


class StoreMemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = StoreMember
        fields = ["id", "user", "email", "role", "created_at"]
        read_only_fields = ["user", "created_at"]


class StoreMemberCreateSerializer(serializers.Serializer):
    """Add a member to a store by email. Creates the user if they don't exist."""
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150, required=False, default="")
    last_name = serializers.CharField(max_length=150, required=False, default="")
    password = serializers.CharField(max_length=128, required=False, default="")
    confirm_password = serializers.CharField(max_length=128, required=False, default="")
    role = serializers.ChoiceField(choices=StoreMember.Roles.choices)

    def validate(self, attrs):
        password = attrs.get("password", "")
        confirm = attrs.get("confirm_password", "")
        if password != confirm:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})
        if password:
            if len(password) < 8:
                raise serializers.ValidationError({"password": "Password must be at least 8 characters."})
            if not any(c.isalpha() for c in password):
                raise serializers.ValidationError({"password": "Password must contain at least one letter."})
            if not any(c.isdigit() for c in password):
                raise serializers.ValidationError({"password": "Password must contain at least one number."})
        return attrs


class StoreMemberRoleSerializer(serializers.Serializer):
    """Update a member's role."""
    role = serializers.ChoiceField(choices=StoreMember.Roles.choices)


class StorefrontSetupSerializer(serializers.Serializer):
    """Storefront appearance/config applied at store creation.

    The design key triggers an automatic scaffold+deploy pipeline that
    creates a live storefront at <slug>.<configured base domain>."""
    design = serializers.CharField(max_length=50, required=False, default="basic")
    contact_email = serializers.EmailField(required=False, allow_blank=True, default="")
    support_phone = serializers.CharField(required=False, allow_blank=True, default="")
    whatsapp_phone = serializers.CharField(required=False, allow_blank=True, default="")
    social_links = serializers.DictField(
        child=serializers.URLField(), required=False, default=dict,
    )
    homepage_product_source = serializers.ChoiceField(
        choices=StoreConfig.HomepageProductSources.choices,
        required=False,
        default=StoreConfig.HomepageProductSources.FEATURED,
    )
    homepage_product_limit = serializers.IntegerField(min_value=4, max_value=24, required=False, default=8)
    brand_color = serializers.RegexField(
        regex=HEX_COLOR_RE, required=False, default="#1F8A8C",
        error_messages={"invalid": "Enter a 6-digit hex color, e.g. #1F8A8C."},
    )
    currency = serializers.CharField(max_length=8, required=False, default="MAD")

    def validate_design(self, value):
        from api.designs import DesignNotFound, get_design

        try:
            design = get_design(value)
        except DesignNotFound:
            raise serializers.ValidationError(
                "Choose one of the available storefront designs."
            )
        if design.get("status") != "ready":
            raise serializers.ValidationError(
                "This storefront family is still being prepared."
            )
        return value

    def validate_support_phone(self, value):
        return self._validate_phone(value)

    def validate_whatsapp_phone(self, value):
        return self._validate_phone(value)

    def validate_social_links(self, value):
        from .models import SOCIAL_CHANNELS

        unknown = set(value) - SOCIAL_CHANNELS
        if unknown:
            raise serializers.ValidationError(
                f"Unsupported social channels: {', '.join(sorted(unknown))}."
            )
        insecure = [key for key, url in value.items() if not url.lower().startswith("https://")]
        if insecure:
            raise serializers.ValidationError("Social profile links must use HTTPS.")
        return {key: url.strip() for key, url in value.items() if url.strip()}

    @staticmethod
    def _validate_phone(value):
        if not value:
            return ""
        from phonenumber_field.phonenumber import to_python

        phone = to_python(value)
        if not phone or not phone.is_valid():
            raise serializers.ValidationError("Enter a valid international phone number.")
        return phone.as_e164


class StoreDeleteSerializer(serializers.Serializer):
    """Two explicit typed confirmations required for destructive deletion."""

    store_name = serializers.CharField(trim_whitespace=False)
    confirmation = serializers.CharField(trim_whitespace=False)

    def validate(self, attrs):
        store = self.context["store"]
        if attrs["store_name"] != store.slug:
            raise serializers.ValidationError({
                "store_name": f'Type "{store.slug}" exactly to confirm deletion.'
            })
        expected = "delete my store"
        if attrs["confirmation"] != expected:
            raise serializers.ValidationError({
                "confirmation": f'Type "{expected}" exactly to confirm deletion.'
            })
        return attrs


class StoreSerializer(serializers.ModelSerializer):
    from catalog.serializers import RetailTypeSerializer

    members = StoreMemberSerializer(many=True, read_only=True)
    config = StorefrontSetupSerializer(write_only=True, required=False)
    storefront_url = serializers.SerializerMethodField(read_only=True)
    storefront_request_id = serializers.SerializerMethodField(read_only=True)
    storefront_status = serializers.SerializerMethodField(read_only=True)
    storefront_progress = serializers.SerializerMethodField(read_only=True)
    storefront_release_status = serializers.SerializerMethodField(read_only=True)
    plan = serializers.CharField(source="config.plan", read_only=True)
    analytics_level = serializers.SerializerMethodField(read_only=True)
    logo_url = serializers.SerializerMethodField(read_only=True)
    retail_type_detail = RetailTypeSerializer(source="retail_type", read_only=True)

    class Meta:
        model = Store
        fields = [
            "id", "slug", "name", "logo", "logo_url", "retail_type",
            "retail_type_detail", "catalog_config", "members", "storefront_url",
            "storefront_request_id", "storefront_status", "storefront_progress",
            "storefront_release_status",
            "plan", "analytics_level", "config", "created_at",
        ]
        read_only_fields = ["id", "members", "created_at", "retail_type_detail", "catalog_config"]
        # Address conflicts are handled by StoreListCreateView as HTTP 409 so
        # every client receives the same neutral, race-safe response. The
        # database unique constraint remains the final authority.
        extra_kwargs = {"slug": {"validators": []}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from catalog.models import RetailType
        self.fields["retail_type"].queryset = RetailType.objects.all()

    def validate_slug(self, value):
        if not STORE_SLUG_RE.fullmatch(value):
            raise serializers.ValidationError(
                "Use lowercase letters, digits and single hyphens; start with a letter and do not end with a hyphen."
            )
        return value

    def get_storefront_url(self, obj):
        req = self._latest_storefront_request(obj)
        if (
            req
            and req.deployed_url
            and (
                (
                    req.pipeline_state == req.PIPELINE_LIVE
                    and req.status == req.STATUS_CREATED
                    and req.deploy_status == req.DEPLOY_DEPLOYED
                )
                or req.last_known_good_revision
            )
        ):
            return req.deployed_url
        return None

    def get_storefront_request_id(self, obj):
        req = self._latest_storefront_request(obj)
        return req.id if req else None

    def get_storefront_status(self, obj):
        req = self._latest_storefront_request(obj)
        if req is None:
            return "draft"
        if (
            req.status == req.STATUS_PENDING
            and req.deploy_status == req.DEPLOY_NOT_DEPLOYED
            and not req.task_id
        ):
            return "draft"
        # A failed or active update must not make the already-serving release
        # disappear from My Stores. The more precise operation state is
        # returned separately as ``storefront_release_status``.
        if req.deployed_url and req.last_known_good_revision:
            return "live"
        if req.pipeline_state == req.PIPELINE_LIVE and req.deploy_status == req.DEPLOY_DEPLOYED:
            return "live"
        if req.pipeline_state == req.PIPELINE_FAILED or req.deploy_status == req.DEPLOY_FAILED:
            return "failed"
        return "preparing"

    def get_storefront_release_status(self, obj):
        req = self._latest_storefront_request(obj)
        if req is None:
            return "not_created"
        from storefront_factory.release_status import storefront_release_status

        return storefront_release_status(req)

    def get_storefront_progress(self, obj):
        req = self._latest_storefront_request(obj)
        return req.progress if req else 0

    def _latest_storefront_request(self, obj):
        cache_name = "_serialized_latest_storefront_request"
        if not hasattr(obj, cache_name):
            prefetched = getattr(obj, "_prefetched_objects_cache", {}).get("storefront_requests")
            latest = (
                max(prefetched, key=lambda item: (item.created_at, item.pk))
                if prefetched
                else obj.storefront_requests.order_by("-created_at").first()
            )
            setattr(obj, cache_name, latest)
        return getattr(obj, cache_name)

    def get_logo_url(self, obj):
        return media_asset_url(obj.logo, request=self.context.get("request")) or None

    def get_analytics_level(self, obj):
        """Expose the store owner's governed reporting level to the dashboard.

        Team members manage the owner's store, so their personal subscription
        must not accidentally downgrade (or upgrade) what that store can show.
        """
        from subscriptions.entitlements import resolve_entitlement
        from subscriptions.policies import ANALYTICS_LEVEL_KEY

        owner = next(
            (
                membership.user
                for membership in obj.members.all()
                if membership.role == membership.Roles.OWNER
            ),
            None,
        )
        if owner is None:
            return "essential"
        cache = getattr(self, "_analytics_level_by_owner", None)
        if cache is None:
            cache = self._analytics_level_by_owner = {}
        if owner.pk not in cache:
            cache[owner.pk] = resolve_entitlement(owner, ANALYTICS_LEVEL_KEY)
        level = cache[owner.pk]
        return level if level in {"essential", "standard", "advanced"} else "essential"
