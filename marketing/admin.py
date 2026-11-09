from django.contrib import admin

from .models import NewsletterCampaign, Subscriber


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ["store", "email", "status", "subscribed_at", "unsubscribed_at"]
    list_filter = ["store", "status"]
    search_fields = ["email"]


@admin.register(NewsletterCampaign)
class NewsletterCampaignAdmin(admin.ModelAdmin):
    list_display = ["store", "subject", "status", "recipients_count", "sent_count", "failed_count", "sent_at"]
    list_filter = ["store", "status"]
    search_fields = ["subject"]
