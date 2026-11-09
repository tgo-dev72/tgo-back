from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.platform_email import send_platform_email
from store.models import Store, StoreMember
from subscriptions.policies import EntitlementDenied, enforce_operation

from .models import AccessAuditEvent, AccessGroup, AccessRole, MemberAccess, StaffInvitation
from .registry import catalog_payload
from .serializers import AccessGroupSerializer, AccessMemberSerializer, AccessRoleSerializer, AuditEventSerializer, InvitationAcceptSerializer, InvitationSerializer
from .services import audit, effective_fields, effective_permissions, ensure_default_roles, hash_token, issue_token, mark_expired, membership_for, require_permission, validate_delegation, validate_field_permissions


def _store(user, store_id):
    return generics.get_object_or_404(Store.objects.filter(members__user=user), pk=store_id)


class StoreAccessView(APIView):
    permission_classes = [IsAuthenticated]
    required_permission = "staff.view"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.store = _store(request.user, kwargs["store_id"])
        self.actor_membership = membership_for(request.user, self.store)
        if self.actor_membership is None:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("This store membership is inactive.")
        if self.required_permission:
            require_permission(request.user, self.store, self.required_permission)
        ensure_default_roles(self.store, request.user)


class PermissionCatalogView(StoreAccessView):
    required_permission = "roles.view"

    def get(self, request, store_id):
        return Response(catalog_payload())


class EffectivePermissionView(StoreAccessView):
    required_permission = None

    def get(self, request, store_id):
        return Response({
            "store_id": self.store.pk,
            "membership_id": self.actor_membership.pk,
            "permissions": sorted(effective_permissions(self.actor_membership)),
            "field_permissions": effective_fields(self.actor_membership),
        })


class RoleListCreateView(StoreAccessView):
    required_permission = "roles.view"

    def get(self, request, store_id):
        roles = AccessRole.objects.filter(store=self.store).prefetch_related("members")
        return Response(AccessRoleSerializer(roles, many=True).data)

    def post(self, request, store_id):
        require_permission(request.user, self.store, "roles.manage")
        serializer = AccessRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validate_delegation(self.actor_membership, serializer.validated_data.get("permissions", []))
        key = serializer.validated_data.get("key") or slugify(serializer.validated_data["name"])
        if AccessRole.objects.filter(store=self.store, key=key).exists():
            return Response({"key": ["A role with this key already exists."]}, status=400)
        role = serializer.save(store=self.store, key=key, created_by=request.user, is_system=False)
        audit(request, self.store, "role.created", "role", role.pk, after=AccessRoleSerializer(role).data)
        return Response(AccessRoleSerializer(role).data, status=201)


class RoleDetailView(StoreAccessView):
    required_permission = "roles.view"

    def get_role(self, role_id):
        return generics.get_object_or_404(AccessRole.objects.filter(store=self.store), pk=role_id)

    def patch(self, request, store_id, role_id):
        require_permission(request.user, self.store, "roles.manage")
        role = self.get_role(role_id)
        if role.is_system:
            return Response({"detail": "Built-in role templates are protected. Duplicate one to customize it."}, status=409)
        before = AccessRoleSerializer(role).data
        serializer = AccessRoleSerializer(role, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validate_delegation(self.actor_membership, serializer.validated_data.get("permissions", role.permissions))
        role = serializer.save()
        audit(request, self.store, "role.updated", "role", role.pk, before=before, after=AccessRoleSerializer(role).data)
        return Response(AccessRoleSerializer(role).data)

    def delete(self, request, store_id, role_id):
        require_permission(request.user, self.store, "roles.manage")
        role = self.get_role(role_id)
        if role.is_system:
            return Response({"detail": "Built-in role templates cannot be deleted."}, status=409)
        if role.members.exists() or role.access_groups.exists() or role.invitations.filter(status="pending").exists():
            return Response({"detail": "Remove this role from staff, groups, and pending invitations first."}, status=409)
        snapshot = AccessRoleSerializer(role).data
        role_id_value = role.pk
        role.delete()
        audit(request, self.store, "role.deleted", "role", role_id_value, before=snapshot)
        return Response(status=204)


class RoleDuplicateView(StoreAccessView):
    required_permission = "roles.manage"

    def post(self, request, store_id, role_id):
        source = generics.get_object_or_404(AccessRole.objects.filter(store=self.store), pk=role_id)
        validate_delegation(self.actor_membership, source.permissions)
        name = str(request.data.get("name") or f"{source.name} copy")[:100]
        base = slugify(name)[:70] or "custom-role"
        key, suffix = base, 2
        while AccessRole.objects.filter(store=self.store, key=key).exists():
            key, suffix = f"{base}-{suffix}", suffix + 1
        role = AccessRole.objects.create(store=self.store, key=key, name=name, description=source.description, permissions=source.permissions, field_permissions=source.field_permissions, created_by=request.user)
        audit(request, self.store, "role.duplicated", "role", role.pk, after={"source_id": source.pk, "name": name})
        return Response(AccessRoleSerializer(role).data, status=201)


class GroupListCreateView(StoreAccessView):
    required_permission = "groups.view"

    def get(self, request, store_id):
        groups = AccessGroup.objects.filter(store=self.store).prefetch_related("roles", "members")
        return Response(AccessGroupSerializer(groups, many=True, context={"store": self.store}).data)

    def post(self, request, store_id):
        require_permission(request.user, self.store, "groups.manage")
        serializer = AccessGroupSerializer(data=request.data, context={"store": self.store})
        serializer.is_valid(raise_exception=True)
        roles = serializer.validated_data.get("roles", [])
        validate_delegation(self.actor_membership, {permission for role in roles for permission in role.permissions})
        group = serializer.save(store=self.store, created_by=request.user)
        audit(request, self.store, "group.created", "group", group.pk, after=AccessGroupSerializer(group, context={"store": self.store}).data)
        return Response(AccessGroupSerializer(group, context={"store": self.store}).data, status=201)


class GroupDetailView(StoreAccessView):
    required_permission = "groups.view"

    def get_group(self, group_id):
        return generics.get_object_or_404(AccessGroup.objects.filter(store=self.store), pk=group_id)

    def patch(self, request, store_id, group_id):
        require_permission(request.user, self.store, "groups.manage")
        group = self.get_group(group_id)
        before = AccessGroupSerializer(group, context={"store": self.store}).data
        serializer = AccessGroupSerializer(group, data=request.data, partial=True, context={"store": self.store})
        serializer.is_valid(raise_exception=True)
        roles = serializer.validated_data.get("roles", group.roles.all())
        validate_delegation(self.actor_membership, {permission for role in roles for permission in role.permissions})
        group = serializer.save()
        after = AccessGroupSerializer(group, context={"store": self.store}).data
        audit(request, self.store, "group.updated", "group", group.pk, before=before, after=after)
        return Response(after)

    def delete(self, request, store_id, group_id):
        require_permission(request.user, self.store, "groups.manage")
        group = self.get_group(group_id)
        snapshot = AccessGroupSerializer(group, context={"store": self.store}).data
        group.delete()
        audit(request, self.store, "group.deleted", "group", group_id, before=snapshot)
        return Response(status=204)


class MemberListView(StoreAccessView):
    required_permission = "staff.view"

    def get(self, request, store_id):
        members = StoreMember.objects.filter(store=self.store).select_related("user", "access_profile").prefetch_related("access_roles", "access_groups__roles")
        return Response(AccessMemberSerializer(members, many=True, context={"store": self.store}).data)


class MemberDetailView(StoreAccessView):
    required_permission = "staff.view"

    def patch(self, request, store_id, member_id):
        require_permission(request.user, self.store, "staff.update")
        member = generics.get_object_or_404(StoreMember.objects.filter(store=self.store), pk=member_id)
        if member.role == StoreMember.Roles.OWNER:
            return Response({"detail": "Store ownership cannot be changed through access management."}, status=409)
        if member == self.actor_membership:
            return Response({"detail": "You cannot change your own access."}, status=409)
        serializer = AccessMemberSerializer(member, data=request.data, partial=True, context={"store": self.store})
        serializer.is_valid(raise_exception=True)
        roles = serializer.validated_data.pop("access_roles", None)
        groups = serializer.validated_data.pop("access_groups", None)
        direct = serializer.validated_data.pop("direct_permissions", None)
        direct_fields = serializer.validated_data.pop("direct_field_permissions", None)
        active = serializer.validated_data.pop("is_active", None)
        grants = set(direct or [])
        grants.update(permission for role in (roles or []) for permission in role.permissions)
        grants.update(permission for group in (groups or []) for role in group.roles.all() for permission in role.permissions)
        validate_delegation(self.actor_membership, grants)
        if direct_fields is not None:
            try:
                direct_fields = validate_field_permissions(direct_fields)
            except ValueError as exc:
                return Response({"direct_field_permissions": [str(exc)]}, status=400)
        before = AccessMemberSerializer(member, context={"store": self.store}).data
        with transaction.atomic():
            profile, _ = MemberAccess.objects.get_or_create(membership=member)
            if roles is not None:
                member.access_roles.set(roles)
            if groups is not None:
                member.access_groups.set(groups)
            fields = []
            if direct is not None:
                profile.direct_permissions = sorted(set(direct)); fields.append("direct_permissions")
            if direct_fields is not None:
                profile.direct_field_permissions = direct_fields; fields.append("direct_field_permissions")
            if active is not None:
                profile.is_active = active; fields.append("is_active")
            if fields:
                profile.save(update_fields=[*fields, "updated_at"])
        after = AccessMemberSerializer(member, context={"store": self.store}).data
        audit(request, self.store, "member.access_updated", "membership", member.pk, before=before, after=after)
        return Response(after)

    def delete(self, request, store_id, member_id):
        require_permission(request.user, self.store, "staff.revoke")
        member = generics.get_object_or_404(StoreMember.objects.filter(store=self.store), pk=member_id)
        if member.role == StoreMember.Roles.OWNER or member == self.actor_membership:
            return Response({"detail": "Owner and self access cannot be revoked here."}, status=409)
        snapshot = {"email": member.user.email, "membership_id": member.pk}
        member.delete()
        audit(request, self.store, "member.revoked", "membership", member_id, before=snapshot)
        return Response(status=204)


class InvitationListCreateView(StoreAccessView):
    required_permission = "staff.view"

    def get(self, request, store_id):
        mark_expired(StaffInvitation.objects.filter(store=self.store))
        return Response(InvitationSerializer(self.store.staff_invitations.prefetch_related("roles", "groups"), many=True, context={"store": self.store}).data)

    @transaction.atomic
    def post(self, request, store_id):
        require_permission(request.user, self.store, "staff.invite")
        serializer = InvitationSerializer(data=request.data, context={"store": self.store})
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        if self.store.members.filter(user__email__iexact=email).exists():
            return Response({"email": ["This person already belongs to the store."]}, status=400)
        try:
            owner = self.store.members.select_related("user").get(role=StoreMember.Roles.OWNER).user
            # Team seats are account-scoped. Serialize the entitlement check
            # and reservation on the owner row so concurrent invitations
            # cannot both claim the final available seat.
            get_user_model().objects.select_for_update().only("pk").get(pk=owner.pk)
            owned_store_ids = StoreMember.objects.filter(
                user=owner, role=StoreMember.Roles.OWNER,
            ).values_list("store_id", flat=True)
            already_reserved = (
                StoreMember.objects.filter(
                    store_id__in=owned_store_ids, user__email__iexact=email,
                ).exists()
                or StaffInvitation.objects.filter(
                    store_id__in=owned_store_ids,
                    email__iexact=email,
                    status=StaffInvitation.Status.PENDING,
                    expires_at__gt=timezone.now(),
                ).exists()
            )
            enforce_operation(
                owner,
                "team.invite",
                context={"store": self.store, "amount": 0 if already_reserved else 1},
            )
        except EntitlementDenied as exc:
            return Response(exc.as_dict(), status=403)
        roles = serializer.validated_data.get("roles", [])
        groups = serializer.validated_data.get("groups", [])
        validate_delegation(self.actor_membership, {permission for role in roles for permission in role.permissions} | {permission for group in groups for role in group.roles.all() for permission in role.permissions})
        StaffInvitation.objects.filter(store=self.store, email__iexact=email, status="pending").update(status="revoked")
        raw, digest = issue_token()
        invitation = serializer.save(store=self.store, email=email, invited_by=request.user, token_hash=digest, expires_at=timezone.now() + timedelta(days=7))
        url = f"{settings.MERCHANT_DASHBOARD_URL}/accept-invitation?token={raw}"
        send_platform_email(to=email, subject=f"You are invited to {self.store.name}", eyebrow="Team invitation", title=f"Join {self.store.name}", recipient_name=invitation.first_name, paragraphs=[f"{request.user.email} invited you to help manage {self.store.name} on TijaraGO."], action_url=url, action_label="Accept invitation", note="This secure invitation expires in 7 days and can be used once.", headers={"X-TijaraGO-Event": "staff.invited"})
        audit(request, self.store, "invitation.created", "invitation", invitation.public_id, after={"email": email})
        return Response(InvitationSerializer(invitation, context={"store": self.store}).data, status=201)


class InvitationActionView(StoreAccessView):
    required_permission = "staff.invite"

    def post(self, request, store_id, invitation_id, action):
        invitation = generics.get_object_or_404(StaffInvitation.objects.filter(store=self.store), public_id=invitation_id)
        if action == "revoke" and invitation.status == "pending":
            invitation.status = "revoked"; invitation.save(update_fields=["status", "updated_at"])
            audit(request, self.store, "invitation.revoked", "invitation", invitation.public_id, after={"email": invitation.email})
            return Response(InvitationSerializer(invitation, context={"store": self.store}).data)
        if action == "resend" and invitation.status == "pending":
            raw, digest = issue_token()
            invitation.token_hash = digest
            invitation.expires_at = timezone.now() + timedelta(days=7)
            invitation.save(update_fields=["token_hash", "expires_at", "updated_at"])
            url = f"{settings.MERCHANT_DASHBOARD_URL}/accept-invitation?token={raw}"
            send_platform_email(
                to=invitation.email, subject=f"Reminder: join {self.store.name}",
                eyebrow="Team invitation", title=f"Join {self.store.name}",
                recipient_name=invitation.first_name,
                paragraphs=[f"{request.user.email} invited you to help manage {self.store.name} on TijaraGO."],
                action_url=url, action_label="Accept invitation",
                note="This new secure invitation link expires in 7 days and can be used once.",
                headers={"X-TijaraGO-Event": "staff.invitation_resent"},
            )
            audit(request, self.store, "invitation.resent", "invitation", invitation.public_id, after={"email": invitation.email})
            return Response(InvitationSerializer(invitation, context={"store": self.store}).data)
        return Response({"detail": "This invitation cannot perform that action."}, status=409)


class InvitationAcceptView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = InvitationAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invitation = StaffInvitation.objects.filter(token_hash=hash_token(serializer.validated_data["token"]), status="pending").select_related("store").first()
        if not invitation or invitation.expires_at <= timezone.now():
            if invitation:
                invitation.status = "expired"; invitation.save(update_fields=["status", "updated_at"])
            return Response({"detail": "This invitation is invalid or expired."}, status=400)
        User = get_user_model()
        with transaction.atomic():
            invitation = StaffInvitation.objects.select_for_update().select_related("store").get(pk=invitation.pk)
            owner = invitation.store.members.select_related("user").get(role=StoreMember.Roles.OWNER).user
            User.objects.select_for_update().only("pk").get(pk=owner.pk)
            try:
                # This invitation already reserves its seat in the usage
                # counter, so acceptance validates the reservation with no
                # additional requested seat.
                enforce_operation(owner, "team.invite", context={"store": invitation.store, "amount": 0})
            except EntitlementDenied as exc:
                return Response(exc.as_dict(), status=403)
            user = User.objects.filter(email__iexact=invitation.email).first()
            if user is None:
                password = serializer.validated_data.get("password")
                if not password:
                    return Response({"password": ["Choose a password to create your account."]}, status=400)
                user = User.objects.create_user(email=invitation.email, password=password, first_name=serializer.validated_data.get("first_name") or invitation.first_name, last_name=serializer.validated_data.get("last_name") or invitation.last_name)
            member, _ = StoreMember.objects.get_or_create(user=user, store=invitation.store, defaults={"role": StoreMember.Roles.STAFF})
            member.access_roles.set(invitation.roles.all())
            member.access_groups.set(invitation.groups.all())
            MemberAccess.objects.get_or_create(membership=member)
            invitation.status = "accepted"; invitation.accepted_at = timezone.now(); invitation.save(update_fields=["status", "accepted_at", "updated_at"])
            AccessAuditEvent.objects.create(store=invitation.store, actor=user, target_type="invitation", target_id=str(invitation.public_id), action="invitation.accepted", after={"membership_id": member.pk})
        return Response({"detail": "Invitation accepted.", "store_id": invitation.store_id})


class AuditListView(StoreAccessView):
    required_permission = "audit.view"

    def get(self, request, store_id):
        events = AccessAuditEvent.objects.filter(store=self.store).select_related("actor")[:200]
        return Response(AuditEventSerializer(events, many=True).data)
