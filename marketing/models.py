"""Tenant-scoped marketing — ported from legacy store.Subscriber +
NewsletterCampaign (store/email_service.py).

Emails are unique *per store* (the same person can subscribe to several
stores), unlike the legacy global unique email.
"""
import uuid

from django.db import models

from store.models import Store


class Subscriber(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="subscribers")
    email = models.EmailField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    subscribed_at = models.DateTimeField(auto_now_add=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-subscribed_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "email"], name="unique_subscriber_per_store"),
        ]

    def __str__(self):
        return f"{self.store.slug} / {self.email} ({self.status})"


class NewsletterCampaign(models.Model):
    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        FRENCH = "fr", "French"
        ARABIC = "ar", "Arabic"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="campaigns")
    subject = models.CharField(max_length=255)
    preview_text = models.CharField(max_length=255, blank=True)
    body_html = models.TextField(help_text="HTML content of the newsletter body")
    body_text = models.TextField(blank=True, help_text="Plain-text fallback")
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.ENGLISH)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    recipients_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    delivery_task_id = models.UUIDField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.store.slug} / {self.subject} [{self.status}]"


class CampaignDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    campaign = models.ForeignKey(NewsletterCampaign, on_delete=models.CASCADE, related_name="deliveries")
    email = models.EmailField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=500, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["campaign", "email"], name="unique_campaign_recipient"),
        ]
        indexes = [models.Index(fields=["campaign", "status"], name="campaign_delivery_status_idx")]
