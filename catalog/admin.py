from django.contrib import admin

from .models import (
    AttributeDef, CatalogSection, Product, ProductImage, ProductReview,
    ProductVendor, RetailType, StockVariant,
)


@admin.register(RetailType)
class RetailTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "label", "sort_order")
    search_fields = ("key", "label")
    ordering = ("sort_order", "key")


class AttributeDefInline(admin.TabularInline):
    model = AttributeDef
    extra = 0


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 0


class StockVariantInline(admin.TabularInline):
    model = StockVariant
    extra = 0


@admin.register(CatalogSection)
class CatalogSectionAdmin(admin.ModelAdmin):
    list_display = ("store", "key", "label", "parent", "is_visible", "sort_mode", "sort_order")
    list_filter = ("store", "is_visible", "sort_mode")
    search_fields = ("store__slug", "key", "label")
    inlines = [AttributeDefInline]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "store", "ref", "name", "price", "compare_at_price",
        "uses_variants", "show_related_products", "track_inventory", "is_active",
    )
    list_filter = ("store", "is_active", "uses_variants", "show_related_products", "track_inventory")
    search_fields = ("store__slug", "ref", "name", "slug")
    inlines = [ProductImageInline, StockVariantInline]


@admin.register(ProductVendor)
class ProductVendorAdmin(admin.ModelAdmin):
    list_display = ("store", "name", "slug", "updated_at")
    list_filter = ("store",)
    search_fields = ("store__slug", "name", "slug")


@admin.register(ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("store", "product", "customer_name", "rating", "status", "verified_purchase", "created_at")
    list_filter = ("store", "status", "verified_purchase", "rating")
    search_fields = ("product__name", "customer_name", "customer_email")
