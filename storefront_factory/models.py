import re

from django.core.exceptions import ValidationError
from django.db import models

from store.models import Store

SLUG_RE = re.compile(r'^[a-z][a-z0-9]*(-[a-z0-9]+)*$')
HEX_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def validate_app_slug(value):
    if not SLUG_RE.match(value):
        raise ValidationError(
            "Use lowercase letters, digits and single hyphens only (e.g. "
            "'hygiene-store') — this becomes the literal folder name under "
            "apps/, so spaces, underscores, and leading/trailing hyphens "
            "aren't allowed."
        )


def validate_category_key(value):
    if not SLUG_RE.match(value):
        raise ValidationError(
            "Use lowercase letters, digits and single hyphens only (e.g. "
            "'soap-1') — the key is interpolated into generated TypeScript "
            "identifiers, asset filenames and locale keys, so spaces, "
            "underscores, uppercase and special characters aren't allowed."
        )


def validate_hex_color(value):
    if not HEX_COLOR_RE.match(value):
        raise ValidationError("Enter a 6-digit hex color, e.g. #1F8A8C.")


# (react-icons export name, npm subpath) — every entry here was confirmed to
# actually exist in the frontend's installed react-icons version before being
# offered as a choice, so picking one can't produce a broken import.
ICON_CHOICES = [
    ("FaTshirt:fa", "Shirt"),
    ("FaShoePrints:fa", "Shoe / footwear"),
    ("GiSandal:gi", "Sandal"),
    ("GiRunningShoe:gi", "Running shoe"),
    ("FaSocks:fa", "Socks"),
    ("FaGlasses:fa", "Glasses"),
    ("GiSoap:gi", "Soap"),
    ("FaPumpSoap:fa6", "Pump soap / lotion"),
    ("FaBottleDroplet:fa6", "Bottle / droplet"),
    ("FaSprayCan:fa", "Spray can"),
    ("FaTooth:fa", "Tooth"),
    ("GiToothbrush:gi", "Toothbrush"),
    ("GiPerfumeBottle:gi", "Perfume bottle"),
    ("GiLipstick:gi", "Lipstick"),
    ("FaMugHot:fa", "Mug / hot drink"),
    ("FaBoxOpen:fa", "Box"),
    ("FaBagShopping:fa6", "Shopping bag"),
    ("FaGem:fa", "Gem / jewelry"),
    ("FaLeaf:fa", "Leaf"),
    ("FaHeart:fa", "Heart"),
]

ICON_MODULES = {
    "fa": "react-icons/fa",
    "fa6": "react-icons/fa6",
    "gi": "react-icons/gi",
}


class StorefrontRequest(models.Model):
    OPERATION_CREATE = "create"
    OPERATION_UPDATE = "update"
    OPERATION_REDESIGN = "redesign"
    OPERATION_CHOICES = [
        (OPERATION_CREATE, "Create storefront"),
        (OPERATION_UPDATE, "Update storefront release"),
        (OPERATION_REDESIGN, "Change storefront design"),
    ]

    PIPELINE_ACCEPTED = "accepted"
    PIPELINE_PREPARING = "preparing"
    PIPELINE_PUBLISHING = "publishing"
    PIPELINE_PROVISIONING = "provisioning"
    PIPELINE_CONFIGURING_DOMAIN = "configuring_domain"
    PIPELINE_BUILDING = "building"
    PIPELINE_VERIFYING = "verifying"
    PIPELINE_LIVE = "live"
    PIPELINE_FAILED = "failed"
    PIPELINE_STATE_CHOICES = [
        (PIPELINE_ACCEPTED, "Accepted"),
        (PIPELINE_PREPARING, "Preparing storefront"),
        (PIPELINE_PUBLISHING, "Publishing storefront"),
        (PIPELINE_PROVISIONING, "Provisioning hosting"),
        (PIPELINE_CONFIGURING_DOMAIN, "Configuring domain"),
        (PIPELINE_BUILDING, "Building storefront"),
        (PIPELINE_VERIFYING, "Verifying public storefront"),
        (PIPELINE_LIVE, "Live"),
        (PIPELINE_FAILED, "Failed"),
    ]

    STATUS_PENDING = "pending"
    STATUS_SCAFFOLDING = "scaffolding"
    STATUS_CREATED = "created"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SCAFFOLDING, "Scaffolding…"),
        (STATUS_CREATED, "Created"),
        (STATUS_FAILED, "Failed"),
    ]

    name = models.CharField(
        max_length=100,
        help_text="Human-readable store name, e.g. 'Hygiene Store'.",
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        related_name="storefront_requests",
        null=True,
        blank=True,
        help_text="The merchant store this deploy belongs to. Null for legacy/admin-only requests.",
    )
    design = models.CharField(
        max_length=50,
        default="classic",
        help_text="Design template key from design-stock/manifest.json, e.g. 'classic'.",
    )
    slug = models.SlugField(
        max_length=50,
        unique=True,
        validators=[validate_app_slug],
        help_text="Folder + package name under apps/, e.g. 'hygiene-store'.",
    )
    domain = models.CharField(
        max_length=200, blank=True, default="https://CHANGE-ME.example.com",
        help_text="Leave as the placeholder to auto-assign <slug>.STOREFRONT_BASE_DOMAIN at deploy "
                   "time (DNS record + Vercel custom domain created automatically). Set a real value "
                   "here instead if this store needs a fully custom domain.",
    )
    contact_email = models.EmailField(blank=True, default="contact@example.com")
    currency = models.CharField(
        max_length=8,
        default="MAD",
        help_text="ISO 4217 currency code, e.g. MAD, EUR, USD.",
    )

    color_brand = models.CharField(max_length=7, default="#0e92e4", validators=[validate_hex_color])
    color_brand_dark = models.CharField(max_length=7, default="#0b7abf", validators=[validate_hex_color])
    color_brand_deep = models.CharField(max_length=7, default="#0a1c27", validators=[validate_hex_color])
    color_ink = models.CharField(max_length=7, default="#0f1c35", validators=[validate_hex_color])
    color_brand_light = models.CharField(max_length=7, default="#7baccb", validators=[validate_hex_color])
    color_brand_light_1 = models.CharField(max_length=7, default="#f0f7fd", validators=[validate_hex_color])
    color_brand_light_2 = models.CharField(max_length=7, default="#e8f4fd", validators=[validate_hex_color])

    port = models.PositiveIntegerField(
        blank=True, null=True,
        help_text="Dev server port. Leave blank to auto-assign the next free one across existing apps/*.",
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    log = models.TextField(blank=True, editable=False)
    app_path = models.CharField(max_length=300, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    DEPLOY_NOT_DEPLOYED = "not_deployed"
    DEPLOY_DEPLOYING = "deploying"
    DEPLOY_DEPLOYED = "deployed"
    DEPLOY_FAILED = "failed"
    DEPLOY_STATUS_CHOICES = [
        (DEPLOY_NOT_DEPLOYED, "Not deployed"),
        (DEPLOY_DEPLOYING, "Deploying…"),
        (DEPLOY_DEPLOYED, "Deployed"),
        (DEPLOY_FAILED, "Deploy failed"),
    ]
    deploy_status = models.CharField(
        max_length=20, choices=DEPLOY_STATUS_CHOICES, default=DEPLOY_NOT_DEPLOYED,
    )
    vercel_project_id = models.CharField(max_length=100, blank=True, editable=False)
    vercel_deployment_id = models.CharField(max_length=100, blank=True, editable=False)
    github_commit_sha = models.CharField(max_length=64, blank=True, editable=False)
    source_repository = models.CharField(max_length=200, blank=True, editable=False)
    deployment_repository = models.CharField(max_length=200, blank=True, editable=False)
    deployment_branch = models.CharField(max_length=100, blank=True, editable=False)
    deployment_app_path = models.CharField(max_length=200, blank=True, editable=False)
    deployment_layout = models.CharField(
        max_length=16, default="legacy", choices=[("legacy", "Legacy workspace"), ("isolated", "Isolated bundle")],
        editable=False,
    )
    operation = models.CharField(
        max_length=16,
        choices=OPERATION_CHOICES,
        default=OPERATION_CREATE,
        editable=False,
    )
    # ``target/deployed`` refer to the immutable design-stock source release,
    # while ``github_commit_sha`` is the generated commit containing this
    # store's scaffold. Keeping both prevents unrelated store commits from
    # making every existing storefront appear outdated.
    target_revision = models.CharField(max_length=64, blank=True, editable=False)
    deployed_revision = models.CharField(max_length=64, blank=True, editable=False)
    last_known_good_revision = models.CharField(max_length=64, blank=True, editable=False)
    last_known_good_deployment_id = models.CharField(max_length=100, blank=True, editable=False)
    cloudflare_dns_record_id = models.CharField(max_length=64, blank=True, editable=False)
    deployed_url = models.URLField(max_length=200, blank=True, editable=False)
    deploy_log = models.TextField(blank=True, editable=False)
    pipeline_state = models.CharField(
        max_length=32,
        choices=PIPELINE_STATE_CHOICES,
        default=PIPELINE_ACCEPTED,
        db_index=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    public_message = models.CharField(max_length=200, blank=True, default="")
    internal_error = models.TextField(blank=True, default="", editable=False)
    task_id = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    attempt_count = models.PositiveIntegerField(default=0, editable=False)
    event_version = models.PositiveBigIntegerField(default=0, editable=False)
    started_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True, editable=False)
    completed_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.slug})"


class StorefrontRepositoryMigration(models.Model):
    """Operator-owned migration journal; never contains credentials/business data."""

    request = models.OneToOneField(
        StorefrontRequest, on_delete=models.CASCADE, related_name="repository_migration",
    )
    state = models.CharField(max_length=32, default="prepared")
    source = models.JSONField(default=dict)
    destination = models.JSONField(default=dict)
    candidate_deployment_id = models.CharField(max_length=100, blank=True)
    candidate_url = models.URLField(max_length=300, blank=True)
    action_token = models.UUIDField(null=True, blank=True, editable=False)
    action_started_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    journal = models.JSONField(default=list)
    rollback_verified_at = models.DateTimeField(null=True, blank=True)
    retain_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class StorefrontDeploymentEvent(models.Model):
    """Immutable, merchant-safe deployment state history used by SSE clients."""

    storefront = models.ForeignKey(
        StorefrontRequest,
        related_name="deployment_events",
        on_delete=models.CASCADE,
    )
    version = models.PositiveBigIntegerField()
    state = models.CharField(max_length=32, choices=StorefrontRequest.PIPELINE_STATE_CHOICES)
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=200, blank=True, default="")
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["version"]
        constraints = [
            models.UniqueConstraint(
                fields=["storefront", "version"],
                name="uniq_storefront_deployment_event_version",
            ),
        ]

    def __str__(self):
        return f"{self.storefront_id} v{self.version}: {self.state}"


class StorefrontCategory(models.Model):
    storefront = models.ForeignKey(
        StorefrontRequest, related_name="categories", on_delete=models.CASCADE,
    )
    key = models.SlugField(
        max_length=40,
        validators=[validate_category_key],
        help_text="Must match the backend catalog section slug. Lowercase letters, digits and hyphens only — "
                  "it's interpolated into generated TypeScript and asset filenames.",
    )
    label = models.CharField(max_length=60, help_text="Display label, e.g. 'Soap'.")
    icon = models.CharField(max_length=40, choices=ICON_CHOICES, default=ICON_CHOICES[0][0])

    class Meta:
        verbose_name_plural = "categories"
        constraints = [
            models.UniqueConstraint(fields=["storefront", "key"], name="unique_category_key_per_storefront"),
        ]

    def __str__(self):
        return self.label
