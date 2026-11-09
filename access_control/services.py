import hashlib
import secrets

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import get_object_or_404

from store.models import Store, StoreMember

from .models import AccessAuditEvent, AccessRole, MemberAccess
from .registry import DEFAULT_ROLES, FIELD_CATALOG, PERMISSION_CATALOG


LEGACY_ROLE_PERMISSIONS = {
    StoreMember.Roles.ADMIN: {key for key in PERMISSION_CATALOG if key != "billing.manage"},
    StoreMember.Roles.STAFF: {
        "products.view", "products.create", "products.update", "collections.view",
        "collections.manage", "inventory.view", "inventory.update", "orders.view",
        "orders.update", "orders.fulfill", "customers.view", "analytics.view",
        "storefront.view", "mailbox.view", "mailbox.send",
    },
    StoreMember.Roles.DELIVERY: {"orders.view", "orders.fulfill"},
}


def membership_for(user, store, *, active=True):
    membership = StoreMember.objects.filter(user=user, store=store).first()
    if membership is None:
        return None
    if active:
        profile = MemberAccess.objects.filter(membership=membership).first()
        if profile is not None and not profile.is_active:
            return None
    return membership


def _role_field_defaults(permissions):
    result = {}
    for resource, fields in FIELD_CATALOG.items():
        can_view = f"{resource}.view" in permissions
        can_write = f"{resource}.update" in permissions
        if resource == "products":
            can_write = can_write or "inventory.update" in permissions
        if not can_view and not can_write:
            continue
        result[resource] = {
            key: {"view": bool(can_view or can_write), "edit": bool(can_write)}
            for key in fields
        }
    return result


@transaction.atomic
def ensure_default_roles(store, actor=None):
    roles = []
    for key, spec in DEFAULT_ROLES.items():
        role, _ = AccessRole.objects.get_or_create(
            store=store,
            key=key,
            defaults={
                "name": spec["name"], "description": "TijaraGO role template",
                "permissions": sorted(spec["permissions"]),
                "field_permissions": _role_field_defaults(set(spec["permissions"])),
                "is_system": True, "created_by": actor,
            },
        )
        roles.append(role)
    return roles


def effective_permissions(membership):
    if membership.role == StoreMember.Roles.OWNER:
        return set(PERMISSION_CATALOG)
    permissions = set()
    try:
        profile = membership.access_profile
    except MemberAccess.DoesNotExist:
        profile = None
    direct = set(profile.direct_permissions or []) if profile is not None else set()
    roles = list(membership.access_roles.filter(is_active=True))
    group_roles = list(
        AccessRole.objects.filter(access_groups__members=membership, access_groups__is_active=True, is_active=True).distinct()
    )
    has_explicit_access = bool(roles or group_roles or direct or profile is not None)
    if not has_explicit_access:
        permissions.update(LEGACY_ROLE_PERMISSIONS.get(membership.role, set()))
    if profile is not None:
        permissions.update(profile.direct_permissions or [])
    roles.extend(group_roles)
    for role in roles:
        permissions.update(role.permissions or [])
    return permissions.intersection(PERMISSION_CATALOG)


def effective_fields(membership):
    if membership.role == StoreMember.Roles.OWNER:
        return {resource: {key: {"view": True, "edit": True} for key in fields} for resource, fields in FIELD_CATALOG.items()}
    result = {}
    sources = list(membership.access_roles.filter(is_active=True))
    sources.extend(AccessRole.objects.filter(access_groups__members=membership, access_groups__is_active=True, is_active=True).distinct())
    try:
        profile = membership.access_profile
        direct = profile.direct_field_permissions
        direct_permissions = profile.direct_permissions
    except MemberAccess.DoesNotExist:
        direct = {}
        direct_permissions = []
    if not sources and not direct and not direct_permissions:
        result = _role_field_defaults(LEGACY_ROLE_PERMISSIONS.get(membership.role, set()))
    mappings = []
    for role in sources:
        mappings.append(role.field_permissions or _role_field_defaults(set(role.permissions or [])))
    if direct:
        mappings.append(direct)
    elif direct_permissions:
        mappings.append(_role_field_defaults(set(direct_permissions)))
    for mapping in mappings:
        for resource, fields in mapping.items():
            if resource not in FIELD_CATALOG or not isinstance(fields, dict):
                continue
            target = result.setdefault(resource, {})
            for field, rule in fields.items():
                if field not in FIELD_CATALOG[resource] or not isinstance(rule, dict):
                    continue
                current = target.setdefault(field, {"view": False, "edit": False})
                current["view"] = current["view"] or bool(rule.get("view")) or bool(rule.get("edit"))
                current["edit"] = current["edit"] or bool(rule.get("edit"))
    return result


def has_permission(user, store, permission):
    membership = membership_for(user, store)
    return bool(membership and permission in effective_permissions(membership))


def require_permission(user, store, permission):
    membership = membership_for(user, store)
    if membership is None or permission not in effective_permissions(membership):
        raise PermissionDenied("You do not have permission to perform this action for this store.")
    return membership


def permitted_store(user, store_id, permission):
    """Resolve a tenant without exposing another store, then enforce RBAC."""
    store = get_object_or_404(Store.objects.filter(members__user=user), pk=store_id)
    if not has_permission(user, store, permission):
        # Merchant resources conceal tenant existence and access configuration.
        raise Http404
    return store


def validate_delegation(actor_membership, permissions):
    requested = set(permissions)
    unknown = requested.difference(PERMISSION_CATALOG)
    if unknown:
        raise PermissionDenied(f"Unknown or non-delegable permissions: {', '.join(sorted(unknown))}")
    if actor_membership.role != StoreMember.Roles.OWNER:
        allowed = effective_permissions(actor_membership)
        excess = requested.difference(allowed)
        if excess:
            raise PermissionDenied("You cannot delegate permissions you do not possess.")


def validate_field_permissions(value):
    if not isinstance(value, dict):
        raise ValueError("Field permissions must be an object.")
    cleaned = {}
    for resource, fields in value.items():
        if resource not in FIELD_CATALOG or not isinstance(fields, dict):
            raise ValueError("Unknown field-permission resource.")
        cleaned[resource] = {}
        for field, rule in fields.items():
            if field not in FIELD_CATALOG[resource] or not isinstance(rule, dict):
                raise ValueError("Unknown field-permission entry.")
            edit = bool(rule.get("edit"))
            cleaned[resource][field] = {"view": bool(rule.get("view")) or edit, "edit": edit}
    return cleaned


def require_fields(user, store, resource, fields, mode="edit"):
    membership = membership_for(user, store)
    if membership is None:
        raise PermissionDenied("Store membership is required.")
    rules = effective_fields(membership).get(resource, {})
    forbidden = sorted(field for field in fields if not rules.get(field, {}).get(mode, False))
    if forbidden:
        raise PermissionDenied(f"Restricted {resource} fields: {', '.join(forbidden)}")


def audit(request, store, action, target_type, target_id="", before=None, after=None):
    return AccessAuditEvent.objects.create(
        store=store, actor=request.user, action=action, target_type=target_type,
        target_id=str(target_id or ""), before=before or {}, after=after or {},
        correlation_id=request.headers.get("X-Request-ID", "")[:80],
    )


def issue_token():
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def mark_expired(queryset):
    queryset.filter(status="pending", expires_at__lte=timezone.now()).update(status="expired")
