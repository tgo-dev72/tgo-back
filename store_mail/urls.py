from django.urls import path

from .views import (
    MailAttachmentDownloadView, MailboxSummaryView, MailComposeView, MailConversationDetailView,
    MailConversationListView, MailReplyView,
)

urlpatterns = [
    path("<int:store_id>/mailbox/", MailboxSummaryView.as_view(), name="store-mail-summary"),
    path("<int:store_id>/mailbox/conversations/", MailConversationListView.as_view(), name="store-mail-list"),
    path("<int:store_id>/mailbox/conversations/<uuid:conversation_id>/", MailConversationDetailView.as_view(), name="store-mail-detail"),
    path("<int:store_id>/mailbox/conversations/<uuid:conversation_id>/reply/", MailReplyView.as_view(), name="store-mail-reply"),
    path("<int:store_id>/mailbox/compose/", MailComposeView.as_view(), name="store-mail-compose"),
    path("<int:store_id>/mailbox/attachments/<int:attachment_id>/", MailAttachmentDownloadView.as_view(), name="store-mail-attachment"),
]
