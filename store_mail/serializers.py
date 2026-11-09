from django.urls import reverse
from rest_framework import serializers

from .models import MailAttachment, MailConversation, MailMessage


class MailAttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = MailAttachment
        fields = ["id", "filename", "content_type", "size", "content_id", "url"]

    def get_url(self, obj):
        request = self.context.get("request")
        if not request or not obj.file:
            return ""
        path = reverse("store-mail-attachment", args=[obj.message.conversation.store_id, obj.pk])
        return request.build_absolute_uri(path)


class MailMessageSerializer(serializers.ModelSerializer):
    attachments = MailAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = MailMessage
        fields = [
            "public_id", "direction", "status", "sender_email", "sender_name",
            "recipient_email", "subject", "text_body", "html_body", "message_id",
            "in_reply_to", "attachments", "created_at",
        ]


class MailConversationSerializer(serializers.ModelSerializer):
    latest_message = serializers.SerializerMethodField()

    class Meta:
        model = MailConversation
        fields = [
            "public_id", "correspondent_email", "correspondent_name", "subject",
            "status", "unread_count", "last_message_at", "latest_message",
        ]

    def get_latest_message(self, obj):
        message = obj.messages.order_by("-created_at", "-id").first()
        return MailMessageSerializer(message, context=self.context).data if message else None


class MailConversationDetailSerializer(MailConversationSerializer):
    messages = MailMessageSerializer(many=True, read_only=True)

    class Meta(MailConversationSerializer.Meta):
        fields = MailConversationSerializer.Meta.fields + ["messages"]


class ComposeMailSerializer(serializers.Serializer):
    to = serializers.EmailField()
    subject = serializers.CharField(max_length=998)
    text = serializers.CharField(required=False, allow_blank=True, max_length=200_000)
    html = serializers.CharField(required=False, allow_blank=True, max_length=500_000)

    def validate(self, attrs):
        if not (attrs.get("text", "").strip() or attrs.get("html", "").strip()):
            raise serializers.ValidationError("A message body is required.")
        return attrs


class ReplyMailSerializer(serializers.Serializer):
    text = serializers.CharField(required=False, allow_blank=True, max_length=200_000)
    html = serializers.CharField(required=False, allow_blank=True, max_length=500_000)

    def validate(self, attrs):
        if not (attrs.get("text", "").strip() or attrs.get("html", "").strip()):
            raise serializers.ValidationError("A reply body is required.")
        return attrs
