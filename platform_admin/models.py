"""Small, platform-owned audit records."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


PLATFORM_SOCIAL_CHANNELS = frozenset({
    "facebook", "instagram", "linkedin", "tiktok", "x", "youtube",
})


def validate_platform_social_links(value):
    if not isinstance(value, dict):
        raise ValidationError("Social links must be an object keyed by channel.")
    validator = URLValidator(schemes=["https"])
    for channel, url in value.items():
        if channel not in PLATFORM_SOCIAL_CHANNELS:
            raise ValidationError(f"Unsupported social channel: {channel}.")
        if not isinstance(url, str):
            raise ValidationError(f"The {channel} social link must be a URL.")
        validator(url)


class PlatformPresentation(models.Model):
    """Singleton public contact profile rendered by the TijaraGO showroom."""

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    contact_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=32, blank=True, default="")
    whatsapp_phone = models.CharField(max_length=32, blank=True, default="")
    social_links = models.JSONField(
        default=dict,
        blank=True,
        validators=[validate_platform_social_links],
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_presentation_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "platform showroom profile"

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.filter(singleton_key=1).first() or cls(singleton_key=1)


class PlatformMonitoringSettings(models.Model):
    """Singleton delivery policy for operational alert emails."""

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    enabled = models.BooleanField(default=True)
    alert_email = models.EmailField(default="tijarago.services@gmail.com")
    reminder_minutes = models.PositiveIntegerField(default=60)
    last_alert_signature = models.CharField(max_length=64, blank=True, default="")
    last_alert_sent_at = models.DateTimeField(null=True, blank=True)
    last_test_sent_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_monitoring_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "platform monitoring settings"

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.filter(singleton_key=1).first() or cls(singleton_key=1)


class PlatformCommerceSettings(models.Model):
    """Singleton commercial policy applied when a merchant starts Free."""

    singleton_key = models.PositiveSmallIntegerField(default=1, unique=True, editable=False)
    free_trial_days = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(1), MaxValueValidator(365)],
        help_text="Number of days a newly granted Free trial remains usable.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_commerce_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "platform commerce settings"

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        self.full_clean()
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.filter(singleton_key=1).first() or cls(singleton_key=1)


class UserAdministrationEvent(models.Model):
    """Immutable record of sensitive user-directory changes."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_user_actions",
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_admin_events",
    )
    target_user_id = models.PositiveBigIntegerField()
    target_email = models.EmailField()
    action = models.CharField(max_length=32)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_user_id", "created_at"])]
