from django.contrib import admin

from .models import ClientProfile, Store, StoreConfig, StoreCustomer, StorefrontAsset, StorefrontPage, StoreMember


class StoreMemberInline(admin.TabularInline):
    model = StoreMember
    extra = 0


class StoreConfigInline(admin.StackedInline):
    model = StoreConfig
    extra = 0


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "created_at")
    search_fields = ("slug", "name")
    inlines = [StoreConfigInline, StoreMemberInline]


@admin.register(StoreMember)
class StoreMemberAdmin(admin.ModelAdmin):
    list_display = ("user", "store", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("user__email", "store__slug")


@admin.register(StorefrontPage)
class StorefrontPageAdmin(admin.ModelAdmin):
    list_display = ("store", "page_key", "page_type", "is_visible", "revision", "published_at", "updated_at")
    list_filter = ("page_type", "is_visible", "show_in_navigation")
    search_fields = ("store__slug", "store__name")
    readonly_fields = ("draft_sections", "published_sections", "revision", "published_at", "created_at", "updated_at", "updated_by")


@admin.register(StorefrontAsset)
class StorefrontAssetAdmin(admin.ModelAdmin):
    list_display = ("store", "image", "uploaded_by", "created_at")
    search_fields = ("store__slug", "store__name")
    readonly_fields = ("store", "image", "uploaded_by", "created_at")


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "city", "country", "loyalty_points", "created_at")
    search_fields = ("user__email",)


@admin.register(StoreCustomer)
class StoreCustomerAdmin(admin.ModelAdmin):
    list_display = ("profile", "store", "loyalty_points", "created_at")
    list_filter = ("store",)
    search_fields = ("profile__user__email", "store__slug")
