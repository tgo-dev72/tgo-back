"""Serializers for the Phase 2 showroom API (designs + deploy requests)."""
from rest_framework import serializers

from store.design_access import can_use_design
from store.models import Store, StoreConfig
from storefront_factory.models import StorefrontRequest

from .designs import DesignNotFound, get_design

VALID_HEX_RE = r"^#[0-9a-fA-F]{6}$"


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02X}" for c in rgb)


def _mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))  # type: ignore[return-value]


def derive_palette(brand_hex: str) -> dict[str, str]:
    """Derives the full 7-color palette (StorefrontRequest's color_* fields)
    from a single brand color the merchant picks in the showroom."""
    brand = _hex_to_rgb(brand_hex)
    black = (0, 0, 0)
    white = (255, 255, 255)
    return {
        "color_brand": _to_hex(brand),
        "color_brand_dark": _to_hex(_mix(brand, black, 0.28)),
        "color_brand_deep": _to_hex(_mix(brand, black, 0.55)),
        "color_ink": _to_hex(_mix(brand, black, 0.50)),
        "color_brand_light": _to_hex(_mix(brand, white, 0.45)),
        "color_brand_light_1": _to_hex(_mix(brand, white, 0.85)),
        "color_brand_light_2": _to_hex(_mix(brand, white, 0.93)),
    }


class DesignSerializer(serializers.Serializer):
    key = serializers.CharField()
    name = serializers.CharField()
    tagline = serializers.CharField()
    description = serializers.CharField()
    screenshots = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    demo_url = serializers.CharField(required=False, allow_null=True, default=None)
    preview_available = serializers.BooleanField(required=False, default=False)
    demo_store_slug = serializers.CharField(required=False, allow_null=True, default=None)
    presets = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    preview_ticket = serializers.CharField(required=False, write_only=True)
    tier = serializers.ChoiceField(choices=("standard", "premium"), required=False, default="standard")
    features = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    feature_maturity = serializers.DictField(
        child=serializers.ChoiceField(choices=("production", "beta", "prototype")),
        required=False,
        default=dict,
    )
    family = serializers.CharField(required=False, default="legacy")
    business_need = serializers.CharField(required=False, default="")
    best_for = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    journey = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    business_outcomes = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    # An older manifest must fail closed during a staggered deployment.
    status = serializers.ChoiceField(
        choices=("ready", "building"), required=False, default="building"
    )
    config_schema = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class StorefrontRequestConfigSerializer(serializers.Serializer):
    """The merchant-facing subset of config, driven by the design's
    `config_schema`. Mapped onto the full StorefrontRequest fields."""
    store_name = serializers.CharField(max_length=100)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    brand_color = serializers.RegexField(regex=VALID_HEX_RE, required=False, default="#1F8A8C")
    currency = serializers.CharField(max_length=8, required=False, default="MAD")


class StorefrontRequestSerializer(serializers.ModelSerializer):
    store = serializers.PrimaryKeyRelatedField(queryset=Store.objects.all())
    config = StorefrontRequestConfigSerializer(write_only=True)
    # Provider logs can contain repository/provider identifiers and are kept
    # for platform staff only. Merchant clients receive the safe state model.
    log = serializers.SerializerMethodField()
    deploy_log = serializers.SerializerMethodField()
    release_status = serializers.SerializerMethodField()

    class Meta:
        model = StorefrontRequest
        fields = [
            "id",
            "store",
            "design",
            "name",
            "slug",
            "status",
            "deploy_status",
            "pipeline_state",
            "progress",
            "public_message",
            "event_version",
            "operation",
            "release_status",
            "deployed_url",
            "app_path",
            "port",
            "log",
            "deploy_log",
            "config",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "name",
            "slug",
            "status",
            "deploy_status",
            "pipeline_state",
            "progress",
            "public_message",
            "event_version",
            "operation",
            "release_status",
            "deployed_url",
            "app_path",
            "port",
            "log",
            "deploy_log",
            "created_at",
        ]

    def get_log(self, _obj):
        return ""

    def get_deploy_log(self, _obj):
        return ""

    def get_release_status(self, obj):
        from storefront_factory.release_status import storefront_release_status

        return storefront_release_status(obj)

    def validate_design(self, value):
        try:
            design = get_design(value)
        except DesignNotFound:
            raise serializers.ValidationError(
                f"Unknown design '{value}'. Available: {', '.join(d['key'] for d in _available()) or 'none'}"
            )
        if design.get("status") != "ready":
            raise serializers.ValidationError(
                "This storefront family is still being prepared and cannot be deployed."
            )
        return value

    def validate_store(self, value):
        if not value.slug:
            raise serializers.ValidationError("Store is missing a slug.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        store = attrs.get("store") or getattr(self.instance, "store", None)
        design = attrs.get("design") or getattr(self.instance, "design", None)
        if store is not None and design is not None:
            config, _ = StoreConfig.objects.get_or_create(store=store)
            from subscriptions.services import sync_store_entitlement
            sync_store_entitlement(store)
            config.refresh_from_db(fields=["plan"])
            # A design change must not invent a public contact address when
            # the merchant leaves this optional field out of the request.
            attrs["config"].setdefault("contact_email", config.contact_email)
            if not can_use_design(config.plan, design):
                raise serializers.ValidationError({
                    "design": "This premium design requires an active Premium plan."
                })
        return attrs

    def create(self, validated_data):
        config = validated_data.pop("config")
        store = validated_data["store"]
        palette = derive_palette(config["brand_color"])
        return StorefrontRequest.objects.create(
            store=store,
            design=validated_data["design"],
            name=config["store_name"],
            slug=store.slug,
            contact_email=config["contact_email"],
            currency=config["currency"],
            **palette,
        )

    def replace(self, instance):
        """Reset one store's stable delivery record for a new release.

        Provider project/domain identifiers intentionally survive so retries,
        redesigns, and eventual deletion remain idempotent.  Values tied to a
        particular build are cleared before a new worker can observe the row.
        """
        config = self.validated_data["config"]
        values = {
            "design": self.validated_data["design"],
            "name": config["store_name"],
            "contact_email": config["contact_email"],
            "currency": config["currency"],
            **derive_palette(config["brand_color"]),
            "status": StorefrontRequest.STATUS_PENDING,
            "operation": StorefrontRequest.OPERATION_REDESIGN,
            "deploy_status": StorefrontRequest.DEPLOY_NOT_DEPLOYED,
            "pipeline_state": StorefrontRequest.PIPELINE_ACCEPTED,
            "progress": 0,
            "public_message": "",
            "task_id": None,
            "attempt_count": 0,
            "started_at": None,
            "last_heartbeat_at": None,
            "completed_at": None,
            "internal_error": "",
            "log": "",
            "deploy_log": "",
            "app_path": "",
            "port": None,
            "github_commit_sha": "",
            "vercel_deployment_id": "",
        }
        if instance.deployed_url and not instance.last_known_good_revision:
            instance.last_known_good_revision = instance.deployed_revision or instance.github_commit_sha
            instance.last_known_good_deployment_id = instance.vercel_deployment_id
            values["last_known_good_revision"] = instance.last_known_good_revision
            values["last_known_good_deployment_id"] = instance.last_known_good_deployment_id
        if not instance.last_known_good_revision:
            values["deployed_url"] = ""
        values["target_revision"] = ""
        for field, value in values.items():
            setattr(instance, field, value)
        instance.save(update_fields=list(values))
        self.instance = instance
        return instance


def _available():
    from .designs import list_designs

    return list_designs()
