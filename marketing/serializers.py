from rest_framework import serializers

from .models import NewsletterCampaign


class SubscribeSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ContactInquirySerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160, trim_whitespace=True)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
    subject = serializers.CharField(max_length=180, trim_whitespace=True)
    message = serializers.CharField(max_length=5000, trim_whitespace=True)
    # Honeypot: bots commonly fill this invisible field; legitimate clients do not.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True)

    def validate_website(self, value):
        if value:
            raise serializers.ValidationError("Invalid submission.")
        return value


class NewsletterCampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = NewsletterCampaign
        fields = [
            "id", "subject", "preview_text", "body_html", "body_text", "language",
            "status", "scheduled_at", "sent_at", "created_at",
            "recipients_count", "sent_count", "failed_count",
        ]
        read_only_fields = [
            "id", "status", "scheduled_at", "sent_at", "created_at",
            "recipients_count", "sent_count", "failed_count",
        ]
