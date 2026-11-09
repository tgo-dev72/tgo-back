from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from accounts.models import AuthUser
from store.models import ClientProfile


class CustomerRegistrationSerializer(serializers.Serializer):
    firstName = serializers.CharField(max_length=150)
    lastName = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=254)
    confirmEmail = serializers.EmailField(max_length=254, required=False)
    password = serializers.CharField(trim_whitespace=False, write_only=True)
    confirmPassword = serializers.CharField(trim_whitespace=False, write_only=True, required=False)
    phone = PhoneNumberField()
    preferredLanguage = serializers.ChoiceField(
        choices=ClientProfile.PreferredLanguage.choices,
        default=ClientProfile.PreferredLanguage.ARABIC,
    )

    def validate_email(self, value):
        return AuthUser.objects.normalize_email(value).strip().lower()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["firstName"] = attrs["firstName"].strip()
        attrs["lastName"] = attrs["lastName"].strip()
        if not attrs["firstName"]:
            raise serializers.ValidationError({"firstName": "This field may not be blank."})
        if not attrs["lastName"]:
            raise serializers.ValidationError({"lastName": "This field may not be blank."})

        confirmation = attrs.get("confirmEmail")
        if confirmation:
            confirmation = AuthUser.objects.normalize_email(confirmation).strip().lower()
            if confirmation != attrs["email"]:
                raise serializers.ValidationError({"confirmEmail": "The email addresses do not match."})
        if attrs.get("confirmPassword") and attrs["confirmPassword"] != attrs["password"]:
            raise serializers.ValidationError({"confirmPassword": "The passwords do not match."})

        candidate = AuthUser(
            email=attrs["email"],
            first_name=attrs["firstName"],
            last_name=attrs["lastName"],
        )
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        user = AuthUser.objects.create_user(
            email=validated_data["email"],
            password=validated_data["password"],
            first_name=validated_data["firstName"],
            last_name=validated_data["lastName"],
            is_active=False,
            email_verification_sent_at=timezone.now(),
        )
        profile = ClientProfile.objects.create(
            user=user,
            phone=validated_data["phone"].as_e164,
            preferred_language=validated_data["preferredLanguage"],
        )
        return profile


class CustomerProfileSerializer(serializers.Serializer):
    firstName = serializers.CharField(max_length=150)
    lastName = serializers.CharField(max_length=150)
    phone = PhoneNumberField(required=False, allow_blank=True)
    address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    city = serializers.CharField(max_length=50, required=False, allow_blank=True)
    country = serializers.CharField(max_length=50, required=False, allow_blank=True)
    preferredLanguage = serializers.ChoiceField(
        choices=ClientProfile.PreferredLanguage.choices,
        required=False,
    )

    def validate_firstName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        return value

    def validate_lastName(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        user = instance.user
        user_fields = []
        if "firstName" in validated_data:
            user.first_name = validated_data.pop("firstName")
            user_fields.append("first_name")
        if "lastName" in validated_data:
            user.last_name = validated_data.pop("lastName")
            user_fields.append("last_name")
        if user_fields:
            user.save(update_fields=user_fields)
        if "phone" in validated_data:
            phone = validated_data.pop("phone")
            instance.phone = phone.as_e164 if phone else ""
            instance.phone_verified_at = None
        for field in ("address", "city", "country", "preferredLanguage"):
            if field not in validated_data:
                continue
            model_field = "preferred_language" if field == "preferredLanguage" else field
            setattr(instance, model_field, validated_data[field].strip() if isinstance(validated_data[field], str) else validated_data[field])
        instance.save()
        return instance
