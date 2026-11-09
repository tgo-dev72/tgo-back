from django.contrib import admin

from .models import Coupon, CouponRedemption


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "store", "discount_type", "value", "availability", "usage_count", "is_active")
    list_filter = ("discount_type", "scope", "is_active")
    search_fields = ("code", "store__name", "store__slug")
    filter_horizontal = ("categories", "products")


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    list_display = ("order", "coupon", "discount_amount", "status", "customer_key", "created_at")
    list_filter = ("status",)
    search_fields = ("order__order_id", "coupon__code", "customer_key")
    readonly_fields = (
        "coupon", "order", "customer_key", "discount_amount", "status",
        "created_at", "consumed_at", "released_at",
    )
