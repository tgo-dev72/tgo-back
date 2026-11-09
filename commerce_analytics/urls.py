from django.urls import path

from .views import AnalyticsMaintenanceView, MerchantAnalyticsView, StorefrontEventCreateView


urlpatterns = [
    path("store/<int:store_id>/analytics/", MerchantAnalyticsView.as_view(), name="merchant-analytics"),
    path("analytics/events/", StorefrontEventCreateView.as_view(), name="storefront-analytics-event"),
    path("internal/analytics-maintenance/", AnalyticsMaintenanceView.as_view(), name="analytics-maintenance"),
]
