from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from store.models import StoreMember

from .models import AccessAuditEvent, AccessGroup, AccessRole, StaffInvitation
from .registry import PERMISSION_CATALOG
from .services import effective_fields, effective_permissions, validate_field_permissions


class AccessRoleSerializer(serializers.ModelSerializer):
    affected_staff_count = serializers.IntegerField(source="members.count", read_only=True)

    class Meta:
        model = AccessRole
        fields = ["id", "key", "name", "description", "permissions", "field_permissions", "is_system", "is_active", "affected_staff_count", "created_at", "updated_at"]
        read_only_fields = ["id", "is_system", "created_at", "updated_at"]
        extra_kwargs = {"key": {"required": False}}

    def validate_permissions(self, value):
        if not isinstance(value, list) or any(item not in PERMISSION_CATALOG for item in value):
            raise serializers.ValidationError("Choose only permissions from the catalog.")
        return sorted(set(value))

    def validate_field_permissions(self, value):
        try:
            return validate_field_permissions(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class AccessGroupSerializer(serializers.ModelSerializer):
    role_ids = serializers.PrimaryKeyRelatedField(source="roles", many=True, queryset=AccessRole.objects.none(), required=False)
    member_ids = serializers.PrimaryKeyRelatedField(source="members", many=True, queryset=StoreMember.objects.none(), required=False)
    inherited_permissions = serializers.SerializerMethodField()

    class Meta:
        model = AccessGroup
        fields = ["id", "name", "description", "is_active", "role_ids", "member_ids", "inherited_permissions", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store:
            self.fields["role_ids"].child_relation.queryset = AccessRole.objects.filter(store=store)
            self.fields["member_ids"].child_relation.queryset = StoreMember.objects.filter(store=store).exclude(role=StoreMember.Roles.OWNER)

    def get_inherited_permissions(self, obj):
        return sorted({permission for role in obj.roles.filter(is_active=True) for permission in role.permissions})


class AccessMemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    role_ids = serializers.PrimaryKeyRelatedField(source="access_roles", many=True, queryset=AccessRole.objects.none(), required=False)
    group_ids = serializers.PrimaryKeyRelatedField(source="access_groups", many=True, queryset=AccessGroup.objects.none(), required=False)
    direct_permissions = serializers.ListField(child=serializers.CharField(), required=False, write_only=True)
    direct_field_permissions = serializers.JSONField(required=False, write_only=True)
    is_active = serializers.BooleanField(required=False, write_only=True)
    effective_permissions = serializers.SerializerMethodField()
    effective_field_permissions = serializers.SerializerMethodField()

    class Meta:
        model = StoreMember
        fields = ["id", "email", "first_name", "last_name", "role", "role_ids", "group_ids", "direct_permissions", "direct_field_permissions", "is_active", "effective_permissions", "effective_field_permissions", "created_at"]
        read_only_fields = ["id", "email", "first_name", "last_name", "role", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store:
            self.fields["role_ids"].child_relation.queryset = AccessRole.objects.filter(store=store, is_active=True)
            self.fields["group_ids"].child_relation.queryset = AccessGroup.objects.filter(store=store, is_active=True)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        profile = getattr(instance, "access_profile", None)
        data["direct_permissions"] = profile.direct_permissions if profile else []
        data["direct_field_permissions"] = profile.direct_field_permissions if profile else {}
        data["is_active"] = profile.is_active if profile else True
        return data

    def get_effective_permissions(self, obj):
        return sorted(effective_permissions(obj))

    def get_effective_field_permissions(self, obj):
        return effective_fields(obj)


class InvitationSerializer(serializers.ModelSerializer):
    role_ids = serializers.PrimaryKeyRelatedField(source="roles", many=True, queryset=AccessRole.objects.none(), required=False)
    group_ids = serializers.PrimaryKeyRelatedField(source="groups", many=True, queryset=AccessGroup.objects.none(), required=False)

    class Meta:
        model = StaffInvitation
        fields = ["public_id", "email", "first_name", "last_name", "status", "role_ids", "group_ids", "expires_at", "created_at"]
        read_only_fields = ["public_id", "status", "expires_at", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store:
            self.fields["role_ids"].child_relation.queryset = AccessRole.objects.filter(store=store, is_active=True)
            self.fields["group_ids"].child_relation.queryset = AccessGroup.objects.filter(store=store, is_active=True)


class InvitationAcceptSerializer(serializers.Serializer):
    token = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    first_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate(self, attrs):
        password = attrs.get("password", "")
        if password:
            try:
                validate_password(password, user=get_user_model()(email="invited@example.invalid"))
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": exc.messages}) from exc
        return attrs


class AuditEventSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source="actor.email", read_only=True, allow_null=True)

    class Meta:
        model = AccessAuditEvent
        fields = ["id", "actor_email", "target_type", "target_id", "action", "before", "after", "correlation_id", "created_at"]
