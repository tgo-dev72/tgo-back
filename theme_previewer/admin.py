from django.contrib import admin

from .models import ThemeDemoStore


@admin.register(ThemeDemoStore)
class ThemeDemoStoreAdmin(admin.ModelAdmin):
    list_display = ("theme_key", "store", "deployment", "deployment_status", "is_enabled", "updated_at")
    list_filter = ("deployment_status", "is_enabled")
    search_fields = ("theme_key", "store__slug", "store__name")
    readonly_fields = ("catalog_revision", "last_validated_at", "created_at", "updated_at")
