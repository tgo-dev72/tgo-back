import base64
import hashlib
import hmac
import re
import time
import uuid
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from store.email_routing import alias_for

from .models import MailAttachment, MailConversation, MailMessage


MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_ATTACHMENTS_BYTES = 20 * 1024 * 1024
BLOCKED_ATTACHMENT_TYPES = {
    "application/x-msdownload", "application/x-dosexec", "application/x-sh",
    "application/x-executable", "application/java-archive",
}


def normalize_subject(subject):
    return re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", (subject or "").strip(), flags=re.I)[:998]


def split_address(value):
    name, address = parseaddr(value or "")
    return name.strip()[:160], address.strip().lower()


def verify_signed_payload(body, *, timestamp, signature, secret):
    if not secret or not timestamp or not signature:
        return False
    try:
        issued = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - issued) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def verify_svix_payload(body, headers, secret):
    if not secret:
        return False
    message_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "").split()
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(item.removeprefix("v1,") == expected for item in signatures)


def _conversation(store, email, name, subject, references):
    referenced = None
    if references:
        referenced = MailMessage.objects.filter(
            conversation__store=store, message_id__in=references,
        ).select_related("conversation").first()
    if referenced:
        return referenced.conversation
    normalized = normalize_subject(subject)
    existing = MailConversation.objects.filter(
        store=store, correspondent_email__iexact=email, subject__iexact=normalized,
        status=MailConversation.Status.OPEN,
    ).first()
    if existing:
        if name and not existing.correspondent_name:
            existing.correspondent_name = name
        return existing
    return MailConversation.objects.create(
        store=store, correspondent_email=email, correspondent_name=name,
        subject=normalized, last_message_at=timezone.now(),
    )


@transaction.atomic
def ingest_inbound(store, payload):
    message_id = str(payload.get("message_id") or "").strip()[:998]
    if message_id:
        existing = MailMessage.objects.filter(message_id=message_id).first()
        if existing:
            return existing, False
    sender_name, sender_email = split_address(payload.get("from"))
    if not sender_email:
        raise ValueError("Inbound sender is missing.")
    subject = str(payload.get("subject") or "")[:998]
    references = [str(item)[:998] for item in (payload.get("references") or []) if item]
    in_reply_to = str(payload.get("in_reply_to") or "")[:998]
    if in_reply_to and in_reply_to not in references:
        references.append(in_reply_to)
    conversation = _conversation(store, sender_email, sender_name, subject, references)
    message = MailMessage.objects.create(
        conversation=conversation, direction=MailMessage.Direction.INBOUND,
        status=MailMessage.Status.RECEIVED, sender_email=sender_email,
        sender_name=sender_name, recipient_email=alias_for(store), subject=subject,
        text_body=str(payload.get("text") or "")[:500_000],
        html_body=str(payload.get("html") or "")[:1_000_000], message_id=message_id,
        in_reply_to=in_reply_to, references=references,
    )
    total = 0
    for item in payload.get("attachments") or []:
        encoded = item.get("content") or ""
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        content_type = str(item.get("content_type") or "application/octet-stream")[:160]
        if len(content) > MAX_ATTACHMENT_BYTES or total + len(content) > MAX_MESSAGE_ATTACHMENTS_BYTES:
            continue
        if content_type.lower() in BLOCKED_ATTACHMENT_TYPES:
            continue
        total += len(content)
        filename = str(item.get("filename") or "attachment")[:255]
        attachment = MailAttachment(
            message=message, filename=filename, content_type=content_type,
            size=len(content), content_id=str(item.get("content_id") or "")[:255],
        )
        attachment.file.save(filename, ContentFile(content), save=True)
    conversation.unread_count += 1
    conversation.last_message_at = message.created_at
    conversation.status = MailConversation.Status.OPEN
    if sender_name:
        conversation.correspondent_name = sender_name
    conversation.save(update_fields=[
        "unread_count", "last_message_at", "status", "correspondent_name", "updated_at",
    ])
    return message, True


def send_store_message(store, user, *, to, subject, text="", html="", conversation=None):
    from_name = store.name
    from_email = alias_for(store)
    message_id = f"<{uuid.uuid4()}@{settings.STORE_EMAIL_DOMAIN}>"
    previous = conversation.messages.order_by("-created_at", "-id").first() if conversation else None
    in_reply_to = previous.message_id if previous else ""
    references = [*(previous.references if previous else []), in_reply_to] if in_reply_to else []
    if conversation is None:
        conversation = _conversation(store, to, "", subject, references)
    record = MailMessage.objects.create(
        conversation=conversation, direction=MailMessage.Direction.OUTBOUND,
        status=MailMessage.Status.QUEUED, sender_email=from_email, sender_name=from_name,
        recipient_email=to, subject=subject, text_body=text, html_body=html,
        message_id=message_id, in_reply_to=in_reply_to, references=references, sent_by=user,
    )
    headers = {"Message-ID": message_id}
    if in_reply_to:
        headers.update({"In-Reply-To": in_reply_to, "References": " ".join(references)})
    email = EmailMultiAlternatives(
        subject=subject, body=text, from_email=formataddr((from_name, from_email)),
        to=[to], headers=headers, reply_to=[from_email],
    )
    if html:
        email.attach_alternative(html, "text/html")
    try:
        email.send(fail_silently=False)
    except Exception:
        record.status = MailMessage.Status.FAILED
        record.save(update_fields=["status"])
        raise
    record.status = MailMessage.Status.SENT
    record.save(update_fields=["status"])
    conversation.last_message_at = record.created_at
    conversation.status = MailConversation.Status.OPEN
    conversation.save(update_fields=["last_message_at", "status", "updated_at"])
    return record


def record_outbound(store, *, to, subject, text="", html="", message_id="", status=MailMessage.Status.SENT):
    """Record email sent by another store service in the shared mailbox ledger."""
    conversation = _conversation(store, to.lower(), "", subject, [])
    values = {
        "conversation": conversation, "direction": MailMessage.Direction.OUTBOUND,
        "status": status, "sender_email": alias_for(store), "sender_name": store.name,
        "recipient_email": to.lower(), "subject": subject, "text_body": text,
        "html_body": html,
    }
    if message_id:
        record, _ = MailMessage.objects.get_or_create(message_id=message_id, defaults=values)
    else:
        record = MailMessage.objects.create(message_id="", **values)
    if record.conversation_id == conversation.id:
        conversation.last_message_at = record.created_at
        conversation.save(update_fields=["last_message_at", "updated_at"])
    return record


def apply_resend_event(payload):
    event_type = str(payload.get("type") or "")
    data = payload.get("data") or {}
    message_id = str(data.get("message_id") or "")
    provider_id = str(data.get("email_id") or "")
    message = MailMessage.objects.filter(message_id=message_id).first() if message_id else None
    if message is None and provider_id:
        message = MailMessage.objects.filter(provider_id=provider_id).first()
    if message is None:
        return False
    mapping = {
        "email.sent": MailMessage.Status.SENT,
        "email.delivered": MailMessage.Status.DELIVERED,
        "email.delivery_delayed": MailMessage.Status.DELAYED,
        "email.bounced": MailMessage.Status.BOUNCED,
        "email.complained": MailMessage.Status.COMPLAINED,
        "email.failed": MailMessage.Status.FAILED,
    }
    if event_type not in mapping:
        return True
    event_at = parse_datetime(str(payload.get("created_at") or "")) or timezone.now()
    if message.provider_event_at and event_at < message.provider_event_at:
        return True
    message.status = mapping[event_type]
    message.provider_id = provider_id or message.provider_id
    message.provider_event_at = event_at
    message.save(update_fields=["status", "provider_id", "provider_event_at"])
    if event_type in {"email.bounced", "email.complained"}:
        from marketing.models import Subscriber
        Subscriber.objects.filter(
            store=message.conversation.store,
            email__iexact=message.recipient_email,
            status=Subscriber.Status.ACTIVE,
        ).update(status=Subscriber.Status.BOUNCED)
    return True
