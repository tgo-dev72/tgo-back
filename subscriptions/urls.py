from django.urls import path

from .views import PayPalOrderCaptureView, PayPalOrderCreateView, PlanCatalogView, SubscriptionCheckoutView, SubscriptionStatusView, SubscriptionWebhookView


urlpatterns = [
    path("plans", PlanCatalogView.as_view(), name="subscription-plans"),
    path("status", SubscriptionStatusView.as_view(), name="subscription-status"),
    path("checkout", SubscriptionCheckoutView.as_view(), name="subscription-checkout"),
    path("paypal/orders", PayPalOrderCreateView.as_view(), name="paypal-order-create"),
    path("paypal/orders/<str:paypal_order_id>/capture", PayPalOrderCaptureView.as_view(), name="paypal-order-capture"),
    path("webhook", SubscriptionWebhookView.as_view(), name="subscription-webhook"),
    path("webhook/<slug:provider>", SubscriptionWebhookView.as_view(), name="subscription-provider-webhook"),
]
