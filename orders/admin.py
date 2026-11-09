from django.contrib import admin

from .models import Order, OrderedProduct, OrderStatusHistory, QuantityException


class OrderedProductInline(admin.TabularInline):
    model = OrderedProduct
    extra = 0


class QuantityExceptionInline(admin.TabularInline):
    model = QuantityException
    extra = 0


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ["from_status", "to_status", "changed_by", "notes", "created_at"]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = [
        "order_id", "store", "amount", "discount_amount", "coupon_code", "status", "is_paid",
        "tracking_number", "carrier", "delivery_person", "created_at",
    ]
    list_filter = ["store", "status", "is_paid", "payment_mode", "exception"]
    search_fields = ["order_id", "customer_name", "customer_email", "customer_phone", "coupon_code"]
    inlines = [OrderedProductInline, QuantityExceptionInline, OrderStatusHistoryInline]


@admin.register(QuantityException)
class QuantityExceptionAdmin(admin.ModelAdmin):
    list_display = ["store", "ref", "name", "delta_quantity", "treated", "order"]
    list_filter = ["store", "treated"]
