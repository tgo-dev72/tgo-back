"""Tenant-scoped storefront payment ledger.

Every payment is tied to an Order, which is tied to a Store: the webhook
resolves the store *via the order*, so a webhook can never touch another
store's money. The gateway and provider token are recorded per attempt so
pending payments remain routable when a merchant changes gateway.
"""
from django.db import models
from django.conf import settings
from decimal import Decimal

from orders.models import Order
from store.models import Store


class PlatformPaymentGateway(models.Model):
    """Platform-owner visibility switch for a registered subscription gateway."""

    code = models.CharField(max_length=40, unique=True)
    is_enabled = models.BooleanField(default=False)
    sandbox_enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="platform_gateway_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code}: {'enabled' if self.is_enabled else 'disabled'}"


class PaymentTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="payment_transactions")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payment_transactions")
    provider = models.CharField(max_length=40, default="youcanpay", db_index=True)
    provider_token = models.CharField(max_length=100, blank=True, default="")
    provider_transaction_id = models.CharField(max_length=100, blank=True, default="")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    currency = models.CharField(max_length=8, default="MAD")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    webhook_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_transaction_id"],
                condition=~models.Q(provider_transaction_id=""),
                name="uniq_storefront_provider_transaction",
            ),
        ]

    def __str__(self):
        return f"{self.order.order_id} / {self.status}"
