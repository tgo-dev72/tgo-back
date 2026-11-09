from django.contrib import admin

from .models import MailAttachment, MailConversation, MailMessage, StoreMailbox


@admin.register(MailConversation)
class MailConversationAdmin(admin.ModelAdmin):
    list_display = ("store", "correspondent_email", "subject", "status", "unread_count", "last_message_at")
    list_filter = ("status", "store")
    search_fields = ("correspondent_email", "subject", "store__slug")


@admin.register(MailMessage)
class MailMessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "direction", "status", "sender_email", "recipient_email", "created_at")
    list_filter = ("direction", "status")
    search_fields = ("message_id", "sender_email", "recipient_email", "subject")


admin.site.register(MailAttachment)
admin.site.register(StoreMailbox)
