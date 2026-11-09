from django.contrib import admin

from .models import PaymentTransaction


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ["order", "store", "provider", "provider_transaction_id", "amount", "currency", "status", "created_at"]
    list_filter = ["provider", "store", "status"]
    search_fields = ["order__order_id", "provider_token", "provider_transaction_id"]
