from django.contrib import admin

from .models import InventorySnapshot, StorefrontEvent


@admin.register(StorefrontEvent)
class StorefrontEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "store", "product", "order", "occurred_at")
    list_filter = ("event_type", "store")
    search_fields = (
        "event_id", "visitor_hash", "session_hash", "store__slug", "product__name", "order__order_id",
    )
    readonly_fields = ("event_id", "visitor_hash", "session_hash", "created_at")


@admin.register(InventorySnapshot)
class InventorySnapshotAdmin(admin.ModelAdmin):
    list_display = ("snapshot_date", "store", "product", "variant", "available")
    list_filter = ("snapshot_date", "store")
    search_fields = ("store__slug", "product__name", "variant__sku")
