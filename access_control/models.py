import uuid

from django.conf import settings
from django.db import models

from store.models import Store, StoreMember


class AccessRole(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="access_roles")
    key = models.SlugField(max_length=80)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    permissions = models.JSONField(default=list)
    field_permissions = models.JSONField(default=dict, blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_store_roles")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    members = models.ManyToManyField(StoreMember, blank=True, related_name="access_roles")

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(fields=["store", "key"], name="uniq_store_access_role_key")]


class AccessGroup(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="access_groups")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    roles = models.ManyToManyField(AccessRole, blank=True, related_name="access_groups")
    members = models.ManyToManyField(StoreMember, blank=True, related_name="access_groups")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_store_groups")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(fields=["store", "name"], name="uniq_store_access_group_name")]


class MemberAccess(models.Model):
    membership = models.OneToOneField(StoreMember, on_delete=models.CASCADE, related_name="access_profile")
    direct_permissions = models.JSONField(default=list, blank=True)
    direct_field_permissions = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class StaffInvitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="staff_invitations")
    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    roles = models.ManyToManyField(AccessRole, blank=True, related_name="invitations")
    groups = models.ManyToManyField(AccessGroup, blank=True, related_name="invitations")
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="sent_staff_invitations")
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["store", "email"], condition=models.Q(status="pending"), name="uniq_pending_store_invitation")]


class AccessAuditEvent(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="access_audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="merchant_access_actions")
    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=80, blank=True, default="")
    action = models.CharField(max_length=80)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["store", "created_at"])]
