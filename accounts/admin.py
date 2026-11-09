from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .forms import AuthUserChangeForm, AuthUserCreationForm
from .models import AuthUser, MerchantProfile


class MerchantProfileInline(admin.StackedInline):
    model = MerchantProfile
    extra = 0
    readonly_fields = ("phone_verified_at", "terms_accepted_at", "created_at")


@admin.register(AuthUser)
class AuthUserAdmin(UserAdmin):
    ordering = ["email"]
    add_form = AuthUserCreationForm
    form = AuthUserChangeForm
    list_display = ["email", "first_name", "last_name", "email_verified_at", "is_staff", "is_active"]
    list_filter = ["is_staff", "is_superuser", "is_active", "email_verified_at"]
    search_fields = ["email", "first_name", "last_name"]
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email_verified_at", "email_verification_sent_at")}),
        ("Permissions", {
            "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions"),
        }),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "first_name", "last_name", "password1", "password2")}),
    )
    filter_horizontal = ("groups", "user_permissions")
    inlines = [MerchantProfileInline]
