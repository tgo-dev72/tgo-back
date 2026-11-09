import uuid
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

from store.models import Store


def default_entitlement_value():
    return False


def default_entitlement_translations():
    return {}


def default_plan_translations():
    return {}


class SubscriptionPlan(models.Model):
    class EntitlementScope(models.TextChoices):
        ALL_OWNED_STORES = "all_owned_stores", "All stores owned by the subscriber"
        ONE_STORE = "one_store", "One selected store"

    name = models.CharField(max_length=80)
    slug = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True, default="")
    translations = models.JSONField(default=default_plan_translations, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0.00"))])
    currency = models.CharField(max_length=3, default="MAD", validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter ISO currency code.")])
    duration_days = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(3650)])
    entitlement_scope = models.CharField(max_length=24, choices=EntitlementScope.choices, default=EntitlementScope.ALL_OWNED_STORES)
    features = models.JSONField(default=list, blank=True)
    grants_premium_designs = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    upgrade_from = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="direct_upgrades",
        help_text="Explicit lower plan in the same commercial upgrade path.",
    )
    revision = models.PositiveIntegerField(default=1, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="subscription_plan_updates")

    class Meta:
        ordering = ["sort_order", "price", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(pk=models.F("upgrade_from")),
                name="subscription_plan_not_own_predecessor",
            ),
        ]

    @property
    def is_free(self):
        """The protected baseline plan, not merely any zero-priced offer."""
        return self.slug == "free"

    def __str__(self):
        return f"{self.name} ({self.price} {self.currency})"

    def clean(self):
        super().clean()
        if not isinstance(self.translations, dict):
            raise ValidationError({"translations": "Translations must be an object."})
        if self.translations:
            translation_errors = {}
            for language in ("en", "fr", "ar"):
                copy = self.translations.get(language)
                if not isinstance(copy, dict):
                    translation_errors[language] = "Provide a name and description."
                    continue
                if not isinstance(copy.get("name"), str) or not copy["name"].strip():
                    translation_errors[language] = "Provide a translated plan name."
                elif not isinstance(copy.get("description"), str) or not copy["description"].strip():
                    translation_errors[language] = "Provide a translated plan description."
            if translation_errors:
                raise ValidationError({"translations": translation_errors})

    def localized_copy(self, language="ar"):
        language = language if language in {"en", "fr", "ar"} else "ar"
        copy = self.translations.get(language) or self.translations.get("ar") or self.translations.get("en") or {}
        return {
            "name": copy.get("name") or self.name,
            "description": copy.get("description") or self.description,
        }


class EntitlementDefinition(models.Model):
    """A controlled capability key that application code knows how to enforce."""

    class ValueType(models.TextChoices):
        BOOLEAN = "boolean", "Boolean"
        INTEGER = "integer", "Integer limit"
        DECIMAL = "decimal", "Decimal limit"
        TEXT = "text", "Text"
        CHOICE = "choice", "Choice"

    class Scope(models.TextChoices):
        ACCOUNT = "account", "Merchant account"
        STORE = "store", "Store"

    key = models.CharField(
        max_length=80,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", "Use a stable dotted key such as designs.premium.")],
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    translations = models.JSONField(default=default_entitlement_translations, blank=True)
    value_type = models.CharField(max_length=16, choices=ValueType.choices)
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.ACCOUNT)
    unit = models.CharField(max_length=40, blank=True, default="")
    allowed_values = models.JSONField(default=list, blank=True)
    default_value = models.JSONField(default=default_entitlement_value)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entitlement_definition_updates",
    )

    class Meta:
        ordering = ["sort_order", "key"]

    def validate_value(self, value):
        if self.value_type == self.ValueType.BOOLEAN and not isinstance(value, bool):
            raise ValidationError("A boolean entitlement requires true or false.")
        if self.value_type == self.ValueType.INTEGER and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValidationError("An integer entitlement requires a non-negative whole number.")
        if self.value_type == self.ValueType.DECIMAL:
            try:
                decimal_value = Decimal(str(value))
            except Exception as exc:
                raise ValidationError("A decimal entitlement requires a valid non-negative number.") from exc
            if decimal_value < 0:
                raise ValidationError("A decimal entitlement requires a valid non-negative number.")
        if self.value_type in {self.ValueType.TEXT, self.ValueType.CHOICE} and not isinstance(value, str):
            raise ValidationError("This entitlement requires a text value.")
        if self.value_type == self.ValueType.CHOICE and value not in self.allowed_values:
            raise ValidationError("Choose one of the configured values.")
        return value

    def clean(self):
        super().clean()
        if not isinstance(self.translations, dict):
            raise ValidationError({"translations": "Translations must be an object."})
        if self.translations:
            translation_errors = {}
            for language in ("en", "fr", "ar"):
                copy = self.translations.get(language)
                if not isinstance(copy, dict):
                    translation_errors[language] = "Provide a name and description."
                    continue
                if not isinstance(copy.get("name"), str) or not copy["name"].strip():
                    translation_errors[language] = "Provide a translated name."
                elif not isinstance(copy.get("description"), str) or not copy["description"].strip():
                    translation_errors[language] = "Provide a translated description."
            if translation_errors:
                raise ValidationError({"translations": translation_errors})
        if not isinstance(self.allowed_values, list) or any(not isinstance(item, str) or not item.strip() for item in self.allowed_values):
            raise ValidationError({"allowed_values": "Allowed values must be a list of non-empty strings."})
        if self.value_type == self.ValueType.CHOICE and not self.allowed_values:
            raise ValidationError({"allowed_values": "Choice capabilities require at least one allowed value."})
        if self.value_type != self.ValueType.CHOICE and self.allowed_values:
            raise ValidationError({"allowed_values": "Allowed values are only valid for choice capabilities."})
        try:
            self.validate_value(self.default_value)
        except ValidationError as exc:
            raise ValidationError({"default_value": exc.messages}) from exc

    def __str__(self):
        return self.key

    def localized_copy(self, language="ar"):
        language = language if language in {"en", "fr", "ar"} else "ar"
        copy = self.translations.get(language) or self.translations.get("ar") or self.translations.get("en") or {}
        return {
            "name": copy.get("name") or self.name,
            "description": copy.get("description") or self.description,
        }


class PlanEntitlement(models.Model):
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.CASCADE, related_name="configured_entitlements")
    definition = models.ForeignKey(EntitlementDefinition, on_delete=models.PROTECT, related_name="plan_values")
    value = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["definition__sort_order", "definition__key"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "definition"], name="uniq_plan_entitlement_definition"),
        ]

    def clean(self):
        super().clean()
        try:
            self.definition.validate_value(self.value)
        except ValidationError as exc:
            raise ValidationError({"value": exc.messages}) from exc

    def __str__(self):
        return f"{self.plan.slug}: {self.definition.key}={self.value}"


class PlanRevision(models.Model):
    """Immutable audit record for every published plan configuration."""

    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="published_plan_revisions",
    )

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "revision"], name="uniq_plan_revision"),
        ]

    def __str__(self):
        return f"{self.plan.slug} v{self.revision}"


class UserSubscription(models.Model):
    class Status(models.TextChoices):
        INACTIVE = "inactive", "Inactive"
        ACTIVE = "active", "Active"
        CANCELED = "canceled", "Canceled"
        EXPIRED = "expired", "Expired"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="platform_subscriptions")
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="subscriptions")
    selected_store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True, related_name="platform_subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.INACTIVE)
    started_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    entitlement_scope = models.CharField(max_length=24, blank=True, default="")
    grants_premium_designs = models.BooleanField(default=False)
    plan_revision = models.PositiveIntegerField(default=1)
    entitlements_snapshot = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(
                    status="active",
                    entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
                ),
                name="uniq_active_account_subscription",
            ),
            models.UniqueConstraint(
                fields=["user", "selected_store"],
                condition=models.Q(
                    status="active",
                    entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
                ),
                name="uniq_active_store_subscription",
            ),
        ]

    def __str__(self):
        return f"{self.user.email}: {self.status}"


class SubscriptionCreditAccount(models.Model):
    """Materialized merchant credit balance; entries below remain the audit truth."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription_credit_accounts",
    )
    currency = models.CharField(max_length=3, validators=[RegexValidator(r"^[A-Z]{3}$", "Use a three-letter ISO currency code.")])
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    reserved = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "currency"], name="uniq_subscription_credit_currency"),
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="subscription_credit_balance_nonnegative"),
            models.CheckConstraint(condition=models.Q(reserved__gte=0), name="subscription_credit_reserved_nonnegative"),
            models.CheckConstraint(condition=models.Q(reserved__lte=models.F("balance")), name="subscription_credit_reserved_within_balance"),
        ]

    @property
    def available(self):
        return max(self.balance - self.reserved, Decimal("0")).quantize(Decimal("1"), rounding=ROUND_DOWN)

    def __str__(self):
        return f"{self.user.email}: {self.balance} {self.currency}"


class SubscriptionPayment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CAPTURED = "captured", "Captured; activation pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"
        DUPLICATE = "duplicate", "Duplicate payment"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscription_payments")
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    selected_store = models.ForeignKey(Store, on_delete=models.SET_NULL, null=True, blank=True, related_name="subscription_payments")
    plan_name = models.CharField(max_length=80)
    gross_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_authorized = models.BooleanField(
        default=False,
        help_text="The merchant explicitly authorized using account credit for this checkout.",
    )
    credit_reserved = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    credit_released_at = models.DateTimeField(null=True, blank=True)
    credit_consumed_at = models.DateTimeField(null=True, blank=True)
    proration_credit = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    proration_snapshot = models.JSONField(default=dict, blank=True)
    currency = models.CharField(max_length=3)
    duration_days = models.PositiveIntegerField()
    entitlement_scope = models.CharField(max_length=24)
    grants_premium_designs = models.BooleanField(default=False)
    plan_revision = models.PositiveIntegerField(default=1)
    entitlements_snapshot = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=40, default="youcanpay", db_index=True)
    sandbox = models.BooleanField(default=True)
    provider_token = models.CharField(max_length=100, blank=True, default="")
    checkout_url = models.URLField(max_length=500, blank=True, default="")
    provider_transaction_id = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    webhook_payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_token"],
                condition=~models.Q(provider_token=""),
                name="uniq_subscription_provider_order",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_transaction_id"],
                condition=~models.Q(provider_transaction_id=""),
                name="uniq_subscription_provider_transaction",
            ),
        ]

    def __str__(self):
        return f"{self.user.email} / {self.plan_name} / {self.status}"


class SubscriptionCreditEntry(models.Model):
    """Immutable posted credit movement. Positive amounts add value; negative amounts spend it."""

    class EntryType(models.TextChoices):
        UNUSED_PERIOD = "unused_period", "Unused subscription period"
        CHECKOUT_APPLIED = "checkout_applied", "Applied to checkout"
        MANUAL_ADJUSTMENT = "manual_adjustment", "Manual adjustment"
        REVERSAL = "reversal", "Reversal"

    account = models.ForeignKey(SubscriptionCreditAccount, on_delete=models.PROTECT, related_name="entries")
    payment = models.ForeignKey(
        SubscriptionPayment,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_entries",
    )
    entry_type = models.CharField(max_length=24, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "entry_type"],
                condition=models.Q(payment__isnull=False),
                name="uniq_subscription_credit_payment_type",
            ),
            models.CheckConstraint(condition=~models.Q(amount=0), name="subscription_credit_entry_nonzero"),
            models.CheckConstraint(condition=models.Q(balance_after__gte=0), name="subscription_credit_entry_balance_nonnegative"),
        ]

    def __str__(self):
        return f"{self.account_id}: {self.amount} ({self.entry_type})"


class SubscriptionWebhookEvent(models.Model):
    class Status(models.TextChoices):
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"

    provider = models.CharField(max_length=40, default="youcanpay", db_index=True)
    event_id = models.CharField(max_length=100)
    event_name = models.CharField(max_length=80)
    payment = models.ForeignKey(
        SubscriptionPayment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
    )
    status = models.CharField(max_length=16, choices=Status.choices)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_id"],
                name="uniq_subscription_provider_event",
            ),
        ]

    def __str__(self):
        return f"{self.event_name} / {self.event_id}"
