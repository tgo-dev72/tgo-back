import uuid

from django.db import models

from catalog.models import Product, StockVariant
from orders.models import Order
from store.models import Store


class StorefrontEvent(models.Model):
    """Privacy-minimal, tenant-scoped storefront interaction.

    ``visitor_hash`` and ``session_hash`` are separate one-way digests generated
    by the API. Raw visitor/session identifiers and customer contact details
    are never stored here.
    Revenue remains sourced from orders; browser events are behavioural only.
    """

    class EventType(models.TextChoices):
        PAGE_VIEW = "page_view", "Page view"
        PRODUCT_VIEW = "product_view", "Product view"
        SEARCH = "search", "Search"
        ADD_TO_CART = "add_to_cart", "Add to cart"
        CHECKOUT_STARTED = "checkout_started", "Checkout started"
        ORDER_SUBMITTED = "order_submitted", "Order submitted"
        ACCOUNT_ENTRY_CLICKED = "account_entry_clicked", "Account entry clicked"
        LOGIN_VIEWED = "login_viewed", "Login viewed"
        REGISTRATION_VIEWED = "registration_viewed", "Registration viewed"
        LOGIN_STARTED = "login_started", "Login started"
        LOGIN_COMPLETED = "login_completed", "Login completed"
        LOGIN_FAILED = "login_failed", "Login failed"
        REGISTRATION_COMPLETED = "registration_completed", "Registration completed"
        PASSWORD_RESET_STARTED = "password_reset_started", "Password reset started"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="analytics_events")
    event_id = models.UUIDField(default=uuid.uuid4)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    visitor_hash = models.CharField(max_length=64, blank=True, default="")
    session_hash = models.CharField(max_length=64, blank=True, default="")
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="analytics_events",
    )
    variant = models.ForeignKey(
        StockVariant, on_delete=models.SET_NULL, null=True, blank=True, related_name="analytics_events",
    )
    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="analytics_events",
    )
    properties = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["store", "event_id"], name="uniq_analytics_event_per_store"),
        ]
        indexes = [
            models.Index(fields=["store", "event_type", "occurred_at"], name="analytics_store_type_time"),
            models.Index(fields=["store", "occurred_at"], name="analytics_store_time"),
            models.Index(fields=["store", "session_hash", "occurred_at"], name="analytics_store_session"),
        ]


class InventorySnapshot(models.Model):
    """End-of-day inventory fact used for historical stock analysis."""

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="inventory_snapshots")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="inventory_snapshots")
    variant = models.ForeignKey(StockVariant, on_delete=models.CASCADE, related_name="inventory_snapshots")
    snapshot_date = models.DateField()
    quantity = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    available = models.PositiveIntegerField(default=0)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-snapshot_date", "variant_id"]
        constraints = [
            models.UniqueConstraint(fields=["store", "variant", "snapshot_date"], name="uniq_inventory_snapshot_day"),
        ]
        indexes = [
            models.Index(fields=["store", "snapshot_date"], name="inventory_store_day"),
        ]
