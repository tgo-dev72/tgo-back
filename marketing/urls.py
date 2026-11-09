from django.urls import path

from .views import (
    CampaignListCreateView,
    CampaignSendView,
    ContactInquiryView,
    SubscribeView,
    UnsubscribeView,
)
from .internal_views import CampaignDeliveryExecuteView

urlpatterns = [
    path("subscribe/", SubscribeView.as_view(), name="subscribe"),
    path("contact/", ContactInquiryView.as_view(), name="storefront-contact"),
    path("unsubscribe/", UnsubscribeView.as_view(), name="unsubscribe"),
    path("unsubscribe/<uuid:code>/", UnsubscribeView.as_view(), name="unsubscribe-opaque"),
    path("store/<int:store_id>/campaigns/", CampaignListCreateView.as_view(), name="campaign-list"),
    path("store/<int:store_id>/campaigns/<uuid:pk>/send/", CampaignSendView.as_view(), name="campaign-send"),
    path("internal/marketing-campaigns/<uuid:pk>/execute", CampaignDeliveryExecuteView.as_view()),
]
