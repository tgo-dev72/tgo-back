from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from subscriptions.serializers import EntitlementDefinitionSerializer, SubscriptionPlanSerializer
from .models import PLATFORM_SOCIAL_CHANNELS, PlatformMonitoringSettings, PlatformPresentation
from .staff_roles import ROLE_CATALOG


class PlatformPresentationSerializer(serializers.ModelSerializer):
    social_links = serializers.DictField(
        child=serializers.URLField(), required=False,
    )

    class Meta:
        model = PlatformPresentation
        fields = ("contact_email", "support_phone", "whatsapp_phone", "social_links", "updated_at")
        read_only_fields = ("updated_at",)

    def validate_social_links(self, value):
        unsupported = sorted(set(value) - PLATFORM_SOCIAL_CHANNELS)
        if unsupported:
            raise serializers.ValidationError(
                f"Unsupported social channels: {', '.join(unsupported)}."
            )
        insecure = sorted(channel for channel, url in value.items() if not url.startswith("https://"))
        if insecure:
            raise serializers.ValidationError("Social profile links must use HTTPS.")
        return {channel: url.strip() for channel, url in value.items() if url.strip()}

    def validate(self, attrs):
        for field in ("support_phone", "whatsapp_phone"):
            value = attrs.get(field)
            if value and (not value.startswith("+") or not value[1:].replace(" ", "").isdigit()):
                raise serializers.ValidationError({field: "Use an international number beginning with +."})
        return attrs


class PlatformMonitoringSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformMonitoringSettings
        fields = (
            "enabled", "alert_email", "reminder_minutes",
            "last_alert_sent_at", "last_test_sent_at", "updated_at",
        )
        read_only_fields = ("last_alert_sent_at", "last_test_sent_at", "updated_at")

    def validate_reminder_minutes(self, value):
        if value not in {15, 30, 60, 180, 360, 720, 1440}:
            raise serializers.ValidationError("Choose a supported reminder interval.")
        return value


class StaffCreateSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    role = serializers.ChoiceField(choices=tuple(ROLE_CATALOG))

    def validate_email(self, value):
        email = get_user_model().objects.normalize_email(value)
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account already uses this email address.")
        return email

    def validate(self, attrs):
        user = get_user_model()(email=attrs.get("email", ""))
        try:
            validate_password(attrs["password"], user=user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs


class StaffUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    role = serializers.ChoiceField(choices=tuple(ROLE_CATALOG), required=False)
    is_active = serializers.BooleanField(required=False)
    password = serializers.CharField(write_only=True, trim_whitespace=False, required=False)

    def validate(self, attrs):
        if "password" in attrs:
            try:
                validate_password(attrs["password"], user=self.context.get("user"))
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs


class AdminUserUpdateSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254, required=False)
    first_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=150, allow_blank=True, required=False)
    is_active = serializers.BooleanField(required=False)
    email_verified = serializers.BooleanField(required=False)

    def validate_email(self, value):
        user = self.context["user"]
        email = get_user_model().objects.normalize_email(value)
        if get_user_model().objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            raise serializers.ValidationError("An account already uses this email address.")
        return email

__all__ = [
    "AdminUserUpdateSerializer", "EntitlementDefinitionSerializer", "PlatformMonitoringSettingsSerializer", "PlatformPresentationSerializer", "StaffCreateSerializer",
    "StaffUpdateSerializer", "SubscriptionPlanSerializer",
]
