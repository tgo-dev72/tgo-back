"""Canonical, merchant-delegable permission and field registry."""

PERMISSION_CATALOG = {
    "products.view": ("Products", "View products"),
    "products.create": ("Products", "Create products"),
    "products.update": ("Products", "Edit products"),
    "products.delete": ("Products", "Delete products"),
    "collections.view": ("Products", "View categories and collections"),
    "collections.manage": ("Products", "Manage categories and collections"),
    "inventory.view": ("Products", "View inventory"),
    "inventory.update": ("Products", "Adjust inventory"),
    "orders.view": ("Orders", "View orders"),
    "orders.update": ("Orders", "Update orders and notes"),
    "orders.fulfill": ("Orders", "Ship and deliver orders"),
    "orders.cancel": ("Orders", "Cancel orders"),
    "customers.view": ("Customers", "View customers"),
    "customers.update": ("Customers", "Edit customer records"),
    "discounts.view": ("Marketing", "View discount codes"),
    "discounts.manage": ("Marketing", "Manage discount codes"),
    "marketing.view": ("Marketing", "View campaigns"),
    "marketing.manage": ("Marketing", "Manage campaigns"),
    "analytics.view": ("Analytics", "View analytics"),
    "storefront.view": ("Storefront", "View storefront configuration"),
    "storefront.customize": ("Storefront", "Customize storefront content and appearance"),
    "storefront.publish": ("Storefront", "Publish storefront changes"),
    "mailbox.view": ("Mailbox", "View store conversations"),
    "mailbox.send": ("Mailbox", "Send and reply to messages"),
    "settings.view": ("Settings", "View store settings"),
    "settings.update": ("Settings", "Update non-sensitive store settings"),
    "billing.view": ("Billing", "View subscription and billing"),
    "billing.manage": ("Billing", "Manage subscription and payment settings"),
    "staff.view": ("Team & access", "View staff and invitations"),
    "staff.invite": ("Team & access", "Invite staff"),
    "staff.update": ("Team & access", "Change staff access"),
    "staff.revoke": ("Team & access", "Suspend or revoke staff access"),
    "roles.view": ("Team & access", "View roles and effective permissions"),
    "roles.manage": ("Team & access", "Create and update roles"),
    "groups.view": ("Team & access", "View groups"),
    "groups.manage": ("Team & access", "Create and update groups"),
    "audit.view": ("Team & access", "View access activity"),
}

FIELD_CATALOG = {
    "products": {
        "name": ("Name", False), "description": ("Description", False),
        "images": ("Images", False), "categories": ("Categories", False),
        "price": ("Selling price", True), "compare_at_price": ("Compare-at price", True),
        "cost_price": ("Supplier cost", True), "inventory": ("Inventory", False),
        "sku": ("SKU and barcode", False), "seo": ("Search metadata", False),
    },
    "orders": {
        "status": ("Order status", False), "customer_identity": ("Customer identity", False),
        "customer_contact": ("Customer contact details", True), "delivery": ("Delivery details", False),
        "totals": ("Order totals", True), "payment": ("Payment status", True),
        "internal_notes": ("Internal notes", True), "items": ("Order items", False),
    },
    "customers": {
        "identity": ("Customer identity", False), "contact": ("Contact details", True),
        "address": ("Delivery address", True), "orders": ("Order history", False),
        "loyalty": ("Loyalty balance", True), "marketing": ("Marketing consent", False),
    },
}

DEFAULT_ROLES = {
    "administrator": {"name": "Administrator", "permissions": [key for key in PERMISSION_CATALOG if key not in {"billing.manage"}]},
    "store_manager": {"name": "Store manager", "permissions": ["products.view", "products.create", "products.update", "collections.view", "collections.manage", "inventory.view", "inventory.update", "orders.view", "orders.update", "orders.fulfill", "customers.view", "analytics.view", "storefront.view", "settings.view", "staff.view"]},
    "product_manager": {"name": "Product manager", "permissions": ["products.view", "products.create", "products.update", "collections.view", "collections.manage", "inventory.view", "inventory.update"]},
    "order_manager": {"name": "Order manager", "permissions": ["orders.view", "orders.update", "orders.fulfill", "orders.cancel", "customers.view", "inventory.view"]},
    "customer_support": {"name": "Customer support", "permissions": ["orders.view", "orders.update", "customers.view", "mailbox.view", "mailbox.send"]},
    "content_editor": {"name": "Content editor", "permissions": ["products.view", "collections.view", "storefront.view", "storefront.customize"]},
    "marketing_manager": {"name": "Marketing manager", "permissions": ["products.view", "customers.view", "discounts.view", "discounts.manage", "marketing.view", "marketing.manage", "analytics.view"]},
    "finance_viewer": {"name": "Finance viewer", "permissions": ["orders.view", "analytics.view", "billing.view"]},
    "read_only_analyst": {"name": "Read-only analyst", "permissions": ["products.view", "inventory.view", "orders.view", "customers.view", "analytics.view"]},
    "delivery_operator": {"name": "Delivery operator", "permissions": ["orders.view", "orders.fulfill"]},
}


def catalog_payload():
    grouped = {}
    for key, (category, description) in PERMISSION_CATALOG.items():
        grouped.setdefault(category, []).append({"key": key, "description": description})
    fields = {
        resource: [{"key": key, "label": label, "sensitive": sensitive} for key, (label, sensitive) in values.items()]
        for resource, values in FIELD_CATALOG.items()
    }
    return {"permissions": grouped, "fields": fields}
