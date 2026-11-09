from django.urls import path

from .views import (
    CancelPaymentView,
    CreatePaymentView,
    PaymentWebhookView,
    RetryPaymentView,
    VerifyPaymentView,
)

urlpatterns = [
    path("payments/", CreatePaymentView.as_view(), name="payment-create"),
    path("payments/retry/", RetryPaymentView.as_view(), name="payment-retry"),
    path("payments/cancel/", CancelPaymentView.as_view(), name="payment-cancel"),
    path("payments/verify/", VerifyPaymentView.as_view(), name="payment-verify"),
    path("payments/webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    path("payments/webhook/<slug:provider>/", PaymentWebhookView.as_view(), name="payment-provider-webhook"),
]
