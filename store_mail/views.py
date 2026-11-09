import json
import logging
import hmac
from datetime import timedelta

from django.conf import settings
from django.http import FileResponse
from django.db.models import Q
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from access_control.services import permitted_store
from store.models import Store

from .models import MailAttachment, MailConversation, StoreMailbox
from .providers import activate_mailbox, configured as provider_configured, disable_mailbox
from .serializers import (
    ComposeMailSerializer, MailConversationDetailSerializer, MailConversationSerializer,
    MailMessageSerializer, ReplyMailSerializer,
)
from .services import apply_resend_event, ingest_inbound, send_store_message, verify_signed_payload, verify_svix_payload

logger = logging.getLogger(__name__)


class MailboxPagination(PageNumberPagination):
    page_size = 30
    max_page_size = 100


class StoreMailboxMixin:
    permission_classes = [IsAuthenticated]

    def store(self, request, store_id, permission="mailbox.view"):
        return permitted_store(request.user, store_id, permission)

    def conversation(self, request, store_id, conversation_id):
        store = self.store(request, store_id)
        return get_object_or_404(
            MailConversation.objects.prefetch_related("messages__attachments"),
            store=store, public_id=conversation_id,
        )

    def active_store(self, request, store_id):
        store = self.store(request, store_id)
        if not StoreMailbox.objects.filter(store=store, enabled=True).exists():
            return store, Response({"detail": "Activate the store mailbox first."}, status=409)
        return store, None


class MailboxSummaryView(StoreMailboxMixin, APIView):
    def get(self, request, store_id):
        store = self.store(request, store_id)
        mailbox, _ = StoreMailbox.objects.get_or_create(store=store)
        conversations = MailConversation.objects.filter(store=store)
        return Response({
            "address": f"{store.slug}@{settings.STORE_EMAIL_DOMAIN}",
            "configured": provider_configured(),
            "enabled": mailbox.enabled,
            "unread": sum(conversations.values_list("unread_count", flat=True)),
            "open": conversations.filter(status=MailConversation.Status.OPEN).count(),
        })

    def post(self, request, store_id):
        store = self.store(request, store_id, "mailbox.send")
        mailbox, _ = StoreMailbox.objects.get_or_create(store=store)
        action = request.data.get("action")
        try:
            if action == "enable":
                mailbox.routing_rule_id = activate_mailbox(store)
                mailbox.enabled = True
                from store.models import StoreConfig
                config, _ = StoreConfig.objects.get_or_create(store=store)
                config.email_forward_active = ""
                config.email_forward_pending = ""
                config.email_forward_rule_id = ""
                config.save(update_fields=[
                    "email_forward_active", "email_forward_pending", "email_forward_rule_id",
                ])
            elif action == "disable":
                disable_mailbox(store)
                mailbox.routing_rule_id = ""
                mailbox.enabled = False
            else:
                return Response({"action": "Unsupported action."}, status=400)
        except Exception:
            logger.exception("Store %s mailbox routing change failed", store.pk)
            return Response({"detail": "The mailbox routing change failed. Try again later."}, status=502)
        mailbox.save(update_fields=["routing_rule_id", "enabled", "updated_at"])
        return self.get(request, store_id)


class MailConversationListView(StoreMailboxMixin, APIView):
    def get(self, request, store_id):
        store = self.store(request, store_id)
        queryset = MailConversation.objects.filter(store=store).prefetch_related("messages__attachments")
        folder = request.query_params.get("folder", "inbox")
        if folder == "archived":
            queryset = queryset.filter(status=MailConversation.Status.ARCHIVED)
        elif folder == "sent":
            queryset = queryset.filter(messages__direction="outbound").distinct()
        else:
            queryset = queryset.filter(status=MailConversation.Status.OPEN)
        query = request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(subject__icontains=query) | Q(correspondent_email__icontains=query)
                | Q(correspondent_name__icontains=query)
            )
        paginator = MailboxPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = MailConversationSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class MailConversationDetailView(StoreMailboxMixin, APIView):
    def get(self, request, store_id, conversation_id):
        conversation = self.conversation(request, store_id, conversation_id)
        if conversation.unread_count:
            conversation.unread_count = 0
            conversation.save(update_fields=["unread_count", "updated_at"])
        return Response(MailConversationDetailSerializer(conversation, context={"request": request}).data)

    def patch(self, request, store_id, conversation_id):
        self.store(request, store_id, "mailbox.send")
        conversation = self.conversation(request, store_id, conversation_id)
        action = request.data.get("action")
        if action == "archive":
            conversation.status = MailConversation.Status.ARCHIVED
        elif action == "restore":
            conversation.status = MailConversation.Status.OPEN
        elif action == "unread":
            conversation.unread_count = max(1, conversation.unread_count)
        elif action == "read":
            conversation.unread_count = 0
        else:
            return Response({"action": "Unsupported action."}, status=400)
        conversation.save(update_fields=["status", "unread_count", "updated_at"])
        return Response(MailConversationDetailSerializer(conversation, context={"request": request}).data)


class MailComposeView(StoreMailboxMixin, APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "store_mail_send"

    def post(self, request, store_id):
        store = self.store(request, store_id, "mailbox.send")
        if not StoreMailbox.objects.filter(store=store, enabled=True).exists():
            return Response({"detail": "Activate the store mailbox first."}, status=409)
        error = None
        if error:
            return error
        serializer = ComposeMailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = send_store_message(store, request.user, **serializer.validated_data)
        except Exception:
            logger.exception("Store %s mailbox compose failed", store.pk)
            return Response({"detail": "The message could not be sent. Try again later."}, status=502)
        return Response(MailMessageSerializer(message, context={"request": request}).data, status=201)


class MailAttachmentDownloadView(StoreMailboxMixin, APIView):
    def get(self, request, store_id, attachment_id):
        store = self.store(request, store_id)
        attachment = get_object_or_404(
            MailAttachment.objects.select_related("message__conversation"),
            pk=attachment_id, message__conversation__store=store,
        )
        response = FileResponse(
            attachment.file.open("rb"), content_type=attachment.content_type or "application/octet-stream",
        )
        filename = attachment.filename.replace('"', "")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class MailReplyView(StoreMailboxMixin, APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "store_mail_send"

    def post(self, request, store_id, conversation_id):
        self.store(request, store_id, "mailbox.send")
        conversation = self.conversation(request, store_id, conversation_id)
        if not StoreMailbox.objects.filter(store=conversation.store, enabled=True).exists():
            return Response({"detail": "Activate the store mailbox first."}, status=409)
        serializer = ReplyMailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subject = conversation.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}" if subject else "Re: Your message"
        try:
            message = send_store_message(
                conversation.store, request.user, to=conversation.correspondent_email,
                subject=subject, conversation=conversation, **serializer.validated_data,
            )
        except Exception:
            logger.exception("Store %s mailbox reply failed", conversation.store_id)
            return Response({"detail": "The reply could not be sent. Try again later."}, status=502)
        return Response(MailMessageSerializer(message, context={"request": request}).data, status=201)


class CloudflareInboundMailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.body
        if not verify_signed_payload(
            body, timestamp=request.headers.get("X-Tijarago-Timestamp", ""),
            signature=request.headers.get("X-Tijarago-Signature", ""),
            secret=settings.MAILBOX_INBOUND_SECRET,
        ):
            return Response({"detail": "Invalid signature."}, status=401)
        try:
            payload = json.loads(body)
            recipient = str(payload.get("to") or "").strip().lower()
            local, domain = recipient.rsplit("@", 1)
            if domain != settings.STORE_EMAIL_DOMAIN:
                raise ValueError("Recipient domain is not managed.")
            store = Store.objects.get(slug=local, mailbox__enabled=True)
            message, created = ingest_inbound(store, payload)
        except Store.DoesNotExist:
            return Response({"detail": "Unknown mailbox."}, status=404)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"id": str(message.public_id), "created": created}, status=201 if created else 200)


class ResendWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.body
        if not verify_svix_payload(body, request.headers, settings.RESEND_WEBHOOK_SECRET):
            return Response({"detail": "Invalid signature."}, status=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Response({"detail": "Invalid JSON."}, status=400)
        apply_resend_event(payload)
        return Response({"ok": True})


class MailRetentionView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not settings.CRON_SECRET:
            return Response({"detail": "Mail retention is not configured."}, status=503)
        if not hmac.compare_digest(supplied, settings.CRON_SECRET):
            return Response({"detail": "Unauthorized."}, status=401)
        cutoff = timezone.now() - timedelta(days=settings.MAILBOX_RETENTION_DAYS)
        conversations = MailConversation.objects.filter(last_message_at__lt=cutoff)
        count = conversations.count()
        conversations.delete()
        return Response({"deleted_conversations": count, "retention_days": settings.MAILBOX_RETENTION_DAYS})
