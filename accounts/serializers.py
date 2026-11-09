from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils import timezone

from rest_framework import serializers
from phonenumber_field.serializerfields import PhoneNumberField

from .models import MerchantProfile

User = get_user_model()


class MerchantRegistrationSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(max_length=254)
    confirm_email = serializers.EmailField(write_only=True, max_length=254)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)
    # Store/business identity belongs to the store-creation journey, not the
    # merchant account registration form. Keep accepting the legacy field so
    # older deployed clients remain compatible while making it optional.
    business_name = serializers.CharField(
        write_only=True,
        max_length=150,
        required=False,
        allow_blank=True,
        default="",
    )
    phone_number = PhoneNumberField(write_only=True)
    preferred_language = serializers.ChoiceField(
        write_only=True,
        choices=MerchantProfile.PreferredLanguage.choices,
        default=MerchantProfile.PreferredLanguage.ARABIC,
    )
    terms_accepted = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = [
            "id", "email", "confirm_email", "password", "confirm_password", "first_name",
            "last_name", "business_name", "phone_number",
            "preferred_language", "terms_accepted",
        ]

    def validate_email(self, value):
        value = User.objects.normalize_email(value).strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Unable to use this email address.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["confirm_email"] = User.objects.normalize_email(
            attrs["confirm_email"],
        ).strip().lower()
        if attrs["email"] != attrs["confirm_email"]:
            raise serializers.ValidationError({
                "confirm_email": "The email addresses do not match. Check them carefully before continuing.",
            })
        for field in ("first_name", "last_name"):
            attrs[field] = attrs[field].strip()
            if not attrs[field]:
                raise serializers.ValidationError({field: "This field may not be blank."})
        attrs["business_name"] = attrs.get("business_name", "").strip()
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "The passwords do not match."})
        if not attrs["terms_accepted"]:
            raise serializers.ValidationError({"terms_accepted": "You must accept the terms to create an account."})
        candidate = User(
            email=attrs["email"],
            first_name=attrs["first_name"],
            last_name=attrs["last_name"],
        )
        validate_password(attrs["password"], user=candidate)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        profile_data = {
            "business_name": validated_data.pop("business_name"),
            "phone_number": validated_data.pop("phone_number").as_e164,
            "preferred_language": validated_data.pop("preferred_language"),
            "terms_accepted_at": timezone.now(),
            "terms_version": settings.MERCHANT_TERMS_VERSION,
        }
        validated_data.pop("confirm_password")
        validated_data.pop("confirm_email")
        validated_data.pop("terms_accepted")
        user = User.objects.create_user(
            is_active=False,
            email_verification_sent_at=timezone.now(),
            **validated_data,
        )
        MerchantProfile.objects.create(user=user, **profile_data)
        return user


# Backwards-compatible import for older callers; merchant registration now has
# an explicit contract rather than exposing the AuthUser model directly.
UserSerializer = MerchantRegistrationSerializer
