from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.db import models


class ThemeDemoStore(models.Model):
    """Platform-owned mapping from a manifest theme to its isolated demo tenant."""

    class DeploymentStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        PREPARED = "prepared", "Prepared"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    theme_key = models.SlugField(max_length=50, unique=True)
    store = models.OneToOneField(
        "store.Store", on_delete=models.PROTECT, related_name="theme_demo_profile",
    )
    deployment = models.OneToOneField(
        "storefront_factory.StorefrontRequest", on_delete=models.PROTECT,
        related_name="theme_demo_profile", null=True, blank=True,
    )
    public_url = models.URLField(max_length=300, blank=True, default="")
    is_enabled = models.BooleanField(default=False)
    deployment_status = models.CharField(
        max_length=16, choices=DeploymentStatus.choices, default=DeploymentStatus.DRAFT,
    )
    catalog_revision = models.CharField(max_length=64, blank=True, default="")
    last_validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("theme_key",)

    def clean(self):
        super().clean()
        if self.store_id:
            from store.models import StoreConfig

            configured_design = StoreConfig.objects.filter(store_id=self.store_id).values_list("design", flat=True).first()
            if configured_design != self.theme_key:
                raise ValidationError({"store": "The demo tenant design must match the registered theme."})
        if self.deployment_id and (
            self.deployment.store_id != self.store_id or self.deployment.design != self.theme_key
        ):
            raise ValidationError({"deployment": "The prepared deployment must target this demo store and theme."})
        if self.public_url:
            parsed = urlparse(self.public_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValidationError({"public_url": "Use a public HTTPS storefront URL without credentials."})
        if self.is_enabled and self.deployment_status != self.DeploymentStatus.READY:
            raise ValidationError({"is_enabled": "Only a validated, ready demo can be public."})
        if self.is_enabled and not self.public_url:
            raise ValidationError({"public_url": "A public URL is required before enabling a demo."})

    def __str__(self):
        return f"{self.theme_key} → {self.store.slug}"
