"""Store-membership queries shared by merchant-facing applications.

Public storefront requests resolve a tenant through ``store.context``. Views
for merchants must instead scope every query through the authenticated user's
``StoreMember`` rows. Keeping that rule here prevents accidental cross-store
access as new dashboard endpoints are added.
"""

from .models import Store, StoreMember
from rest_framework.permissions import BasePermission


OWNER_ROLES = (StoreMember.Roles.OWNER,)
MANAGER_ROLES = (StoreMember.Roles.OWNER, StoreMember.Roles.ADMIN)
OPERATIONS_ROLES = (
    StoreMember.Roles.OWNER,
    StoreMember.Roles.ADMIN,
    StoreMember.Roles.STAFF,
)
ALL_TEAM_ROLES = tuple(value for value, _label in StoreMember.Roles.choices)


def member_stores(user, roles=None):
    """Return only the stores the authenticated user can access."""
    # Keep both predicates in the same ``filter()`` call. Chaining filters
    # across a reverse FK creates two joins, which could let a staff member
    # borrow another member's owner/admin role for the same store.
    if roles is None:
        queryset = Store.objects.filter(members__user=user)
    else:
        queryset = Store.objects.filter(
            members__user=user,
            members__role__in=roles,
        )
    return queryset.distinct()


def is_store_member(user, store, roles=None) -> bool:
    """Return whether ``user`` has any role in ``store``."""
    if store is None:
        return False
    memberships = store.members.filter(user=user)
    if roles is not None:
        memberships = memberships.filter(role__in=roles)
    return memberships.exists()


def store_membership(user, store):
    """Return the user's membership in ``store``, or ``None``."""
    if store is None:
        return None
    return StoreMember.objects.filter(user=user, store=store).first()


class IsMerchantOwner(BasePermission):
    """Allow billing/entitlement operations only to an actual store owner."""

    message = "Only a store owner can manage platform subscriptions."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.store_memberships.filter(role=StoreMember.Roles.OWNER).exists()
        )


class IsMerchantAccount(BasePermission):
    """Allow merchant billing before or after the account creates its first store."""

    message = "Only a merchant account can manage platform subscriptions."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (
                hasattr(user, "merchant_profile")
                or user.store_memberships.filter(role=StoreMember.Roles.OWNER).exists()
            )
        )
