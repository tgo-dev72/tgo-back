from django.urls import path

from .views import CloudflareInboundMailView, MailRetentionView, ResendWebhookView

urlpatterns = [
    path("cloudflare/inbound/", CloudflareInboundMailView.as_view(), name="cloudflare-mail-inbound"),
    path("resend/events/", ResendWebhookView.as_view(), name="resend-mail-events"),
    path("maintenance/", MailRetentionView.as_view(), name="store-mail-maintenance"),
]
