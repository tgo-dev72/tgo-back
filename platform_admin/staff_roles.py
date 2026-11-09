"""Curated platform-staff roles backed by Django groups and permissions."""

from django.contrib.auth.models import Group, Permission


GROUP_PREFIX = "Platform staff: "

PERMISSION_CATALOG = {
    "accounts.view_authuser": ("People directory", "View platform user identities.", "People"),
    "store.view_store": ("Stores", "View merchant stores and ownership.", "Merchants"),
    "store.view_storemember": ("Store teams", "View merchant team membership.", "Merchants"),
    "store.view_storecustomer": ("Store customers", "View customer relationships by store.", "Merchants"),
    "orders.view_order": ("Orders", "View merchant orders.", "Commerce"),
    "orders.change_order": ("Order operations", "Update order workflow state.", "Commerce"),
    "orders.view_quantityexception": ("Stock exceptions", "View stock reconciliation exceptions.", "Commerce"),
    "orders.change_quantityexception": ("Resolve stock exceptions", "Mark stock exceptions as handled.", "Commerce"),
    "catalog.view_product": ("Catalogs", "View merchant product catalogs.", "Commerce"),
    "catalog.view_stockvariant": ("Inventory", "View product inventory.", "Commerce"),
    "payments.view_paymenttransaction": ("Store payments", "View merchant checkout transactions.", "Billing"),
    "payments.view_platformpaymentgateway": ("Payment gateways", "View platform membership payment methods.", "Billing"),
    "payments.change_platformpaymentgateway": ("Manage payment gateways", "Enable or disable customer-visible membership payment methods.", "Billing"),
    "subscriptions.view_subscriptionpayment": ("Membership payments", "View platform membership payments.", "Billing"),
    "subscriptions.view_usersubscription": ("Memberships", "View current merchant memberships.", "Billing"),
    "subscriptions.view_subscriptionplan": ("Plans", "View subscription offers.", "Plans"),
    "subscriptions.add_subscriptionplan": ("Create plans", "Create new subscription offers.", "Plans"),
    "subscriptions.change_subscriptionplan": ("Edit plans", "Change prices, availability and included benefits.", "Plans"),
    "subscriptions.delete_subscriptionplan": ("Remove unused plans", "Delete plans without subscribers or billing history.", "Plans"),
    "subscriptions.view_entitlementdefinition": ("Plan benefits", "View the approved plan-benefit catalog.", "Plans"),
    "subscriptions.add_entitlementdefinition": ("Add approved benefits", "Enable a registered benefit for plan configuration.", "Plans"),
    "subscriptions.change_entitlementdefinition": ("Edit benefit wording", "Change customer-facing benefit wording and defaults.", "Plans"),
    "subscriptions.delete_entitlementdefinition": ("Remove unused benefits", "Delete benefits that have never been assigned.", "Plans"),
    "storefront_factory.view_storefrontrequest": ("Store delivery", "View storefront preparation and release status.", "Delivery"),
    "storefront_factory.change_storefrontrequest": ("Manage store delivery", "Retry or update storefront delivery operations.", "Delivery"),
    "marketing.view_newslettercampaign": ("Campaigns", "View merchant marketing campaigns.", "Marketing"),
    "marketing.view_subscriber": ("Campaign audiences", "View newsletter audiences.", "Marketing"),
}


ROLE_CATALOG = {
    "platform_admin": {
        "name": "Platform administrator",
        "description": "Runs daily platform operations, plans, billing and delivery. Staff-account control remains owner-only.",
        "permissions": tuple(PERMISSION_CATALOG),
    },
    "operations_lead": {
        "name": "Operations lead",
        "description": "Coordinates stores, orders, inventory exceptions and storefront delivery.",
        "permissions": (
            "accounts.view_authuser", "store.view_store", "store.view_storemember",
            "orders.view_order", "orders.change_order", "orders.view_quantityexception",
            "orders.change_quantityexception", "catalog.view_product", "catalog.view_stockvariant",
            "storefront_factory.view_storefrontrequest", "storefront_factory.change_storefrontrequest",
        ),
    },
    "billing_manager": {
        "name": "Billing & plans manager",
        "description": "Manages membership offers and reviews platform and merchant payment activity.",
        "permissions": (
            "accounts.view_authuser", "store.view_store", "payments.view_paymenttransaction",
            "payments.view_platformpaymentgateway", "payments.change_platformpaymentgateway",
            "subscriptions.view_subscriptionpayment", "subscriptions.view_usersubscription",
            "subscriptions.view_subscriptionplan", "subscriptions.add_subscriptionplan",
            "subscriptions.change_subscriptionplan", "subscriptions.delete_subscriptionplan",
            "subscriptions.view_entitlementdefinition", "subscriptions.add_entitlementdefinition",
            "subscriptions.change_entitlementdefinition", "subscriptions.delete_entitlementdefinition",
        ),
    },
    "merchant_support": {
        "name": "Merchant support",
        "description": "Investigates merchant, team, customer and order questions without changing commercial settings.",
        "permissions": (
            "accounts.view_authuser", "store.view_store", "store.view_storemember",
            "store.view_storecustomer", "orders.view_order", "catalog.view_product",
            "payments.view_paymenttransaction", "subscriptions.view_usersubscription",
        ),
    },
    "delivery_specialist": {
        "name": "Store delivery specialist",
        "description": "Monitors and recovers storefront preparation and release operations.",
        "permissions": (
            "store.view_store", "storefront_factory.view_storefrontrequest",
            "storefront_factory.change_storefrontrequest",
        ),
    },
    "analyst": {
        "name": "Read-only analyst",
        "description": "Reviews platform activity without changing operational or commercial data.",
        "permissions": tuple(
            code for code in PERMISSION_CATALOG
            if ".view_" in code
        ),
    },
}


def role_group_name(role_slug):
    return f"{GROUP_PREFIX}{role_slug}"


def role_slug_for_user(user):
    if user.is_superuser:
        return "platform_owner"
    group_name = user.groups.filter(name__startswith=GROUP_PREFIX).values_list("name", flat=True).first()
    return group_name.removeprefix(GROUP_PREFIX) if group_name else None


def sync_role_group(role_slug):
    role = ROLE_CATALOG[role_slug]
    group, _created = Group.objects.get_or_create(name=role_group_name(role_slug))
    requested = []
    for code in role["permissions"]:
        app_label, codename = code.split(".", 1)
        permission = Permission.objects.filter(
            content_type__app_label=app_label,
            codename=codename,
        ).first()
        if permission:
            requested.append(permission)
    group.permissions.set(requested)
    return group


def assign_staff_role(user, role_slug):
    group = sync_role_group(role_slug)
    user.groups.remove(*Group.objects.filter(name__startswith=GROUP_PREFIX))
    user.user_permissions.clear()
    user.groups.add(group)
    for cache_name in ("_perm_cache", "_user_perm_cache", "_group_perm_cache"):
        user.__dict__.pop(cache_name, None)
    return group


def permission_catalog_payload():
    existing = {
        f"{item.content_type.app_label}.{item.codename}"
        for item in Permission.objects.filter(
            content_type__app_label__in={code.split(".", 1)[0] for code in PERMISSION_CATALOG},
        ).select_related("content_type")
    }
    return [
        {
            "code": code,
            "name": name,
            "description": description,
            "category": category,
            "available": code in existing,
        }
        for code, (name, description, category) in PERMISSION_CATALOG.items()
    ]


def role_catalog_payload():
    permissions = {item["code"]: item for item in permission_catalog_payload()}
    roles = [{
        "slug": "platform_owner",
        "name": "Platform owner",
        "description": "Full authority, including staff accounts and role assignment. Reserved for superusers.",
        "permissions": ["*"],
        "assignable": False,
    }]
    roles.extend({
        "slug": slug,
        "name": role["name"],
        "description": role["description"],
        "permissions": [code for code in role["permissions"] if permissions.get(code, {}).get("available")],
        "assignable": True,
    } for slug, role in ROLE_CATALOG.items())
    return roles
