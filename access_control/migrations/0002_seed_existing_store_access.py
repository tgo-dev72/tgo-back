from django.db import migrations


ROLE_PERMISSIONS = {
    "administrator": ["analytics.view", "audit.view", "billing.view", "collections.manage", "collections.view", "customers.update", "customers.view", "discounts.manage", "discounts.view", "groups.manage", "groups.view", "inventory.update", "inventory.view", "mailbox.send", "mailbox.view", "marketing.manage", "marketing.view", "orders.cancel", "orders.fulfill", "orders.update", "orders.view", "products.create", "products.delete", "products.update", "products.view", "roles.manage", "roles.view", "settings.update", "settings.view", "staff.invite", "staff.revoke", "staff.update", "staff.view", "storefront.customize", "storefront.publish", "storefront.view"],
    "store_manager": ["analytics.view", "collections.manage", "collections.view", "customers.view", "inventory.update", "inventory.view", "orders.fulfill", "orders.update", "orders.view", "products.create", "products.update", "products.view", "settings.view", "staff.view", "storefront.view"],
    "product_manager": ["collections.manage", "collections.view", "inventory.update", "inventory.view", "products.create", "products.update", "products.view"],
    "order_manager": ["customers.view", "inventory.view", "orders.cancel", "orders.fulfill", "orders.update", "orders.view"],
    "customer_support": ["customers.view", "mailbox.send", "mailbox.view", "orders.update", "orders.view"],
    "content_editor": ["collections.view", "products.view", "storefront.customize", "storefront.view"],
    "marketing_manager": ["analytics.view", "customers.view", "discounts.manage", "discounts.view", "marketing.manage", "marketing.view", "products.view"],
    "finance_viewer": ["analytics.view", "billing.view", "orders.view"],
    "read_only_analyst": ["analytics.view", "customers.view", "inventory.view", "orders.view", "products.view"],
    "delivery_operator": ["orders.fulfill", "orders.view"],
}

ROLE_NAMES = {key: key.replace("_", " ").title() for key in ROLE_PERMISSIONS}


def seed(apps, schema_editor):
    Store = apps.get_model("store", "Store")
    StoreMember = apps.get_model("store", "StoreMember")
    AccessRole = apps.get_model("access_control", "AccessRole")
    mapping = {"admin": "administrator", "staff": "store_manager", "delivery": "delivery_operator"}
    for store in Store.objects.all().iterator():
        roles = {}
        for key, permissions in ROLE_PERMISSIONS.items():
            role, _ = AccessRole.objects.get_or_create(
                store=store, key=key,
                defaults={"name": ROLE_NAMES[key], "description": "TijaraGO role template", "permissions": permissions, "field_permissions": {}, "is_system": True, "is_active": True},
            )
            roles[key] = role
        for legacy, key in mapping.items():
            for member in StoreMember.objects.filter(store=store, role=legacy):
                roles[key].members.add(member)


def reverse(apps, schema_editor):
    AccessRole = apps.get_model("access_control", "AccessRole")
    AccessRole.objects.filter(is_system=True, description="TijaraGO role template").delete()


class Migration(migrations.Migration):
    dependencies = [("access_control", "0001_initial")]
    operations = [migrations.RunPython(seed, reverse)]
