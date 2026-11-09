import uuid

from django.conf import settings
from django.db import models


class StoreMailbox(models.Model):
    store = models.OneToOneField("store.Store", on_delete=models.CASCADE, related_name="mailbox")
    enabled = models.BooleanField(default=False)
    routing_rule_id = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class MailConversation(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ARCHIVED = "archived", "Archived"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    store = models.ForeignKey("store.Store", on_delete=models.CASCADE, related_name="mail_conversations")
    correspondent_email = models.EmailField()
    correspondent_name = models.CharField(max_length=160, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    unread_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_message_at", "-id"]
        indexes = [
            models.Index(fields=["store", "status", "last_message_at"]),
            models.Index(fields=["store", "correspondent_email"]),
        ]


class MailMessage(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        DELAYED = "delayed", "Delayed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"
        FAILED = "failed", "Failed"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    conversation = models.ForeignKey(MailConversation, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=10, choices=Direction.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    sender_email = models.EmailField()
    sender_name = models.CharField(max_length=160, blank=True, default="")
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=998, blank=True, default="")
    text_body = models.TextField(blank=True, default="")
    html_body = models.TextField(blank=True, default="")
    message_id = models.CharField(max_length=998, blank=True, default="", db_index=True)
    in_reply_to = models.CharField(max_length=998, blank=True, default="")
    references = models.JSONField(default=list, blank=True)
    provider_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    provider_event_at = models.DateTimeField(null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="store_mail_messages",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["message_id"], condition=~models.Q(message_id=""),
                name="uniq_store_mail_message_id",
            ),
        ]
        indexes = [models.Index(fields=["conversation", "created_at"])]


def attachment_upload_to(instance, filename):
    safe_name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:180]
    return f"store-mail/{instance.message.conversation.store_id}/{instance.message.public_id}/{safe_name}"


class MailAttachment(models.Model):
    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=160, blank=True, default="application/octet-stream")
    size = models.PositiveIntegerField()
    content_id = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=attachment_upload_to)
    created_at = models.DateTimeField(auto_now_add=True)
