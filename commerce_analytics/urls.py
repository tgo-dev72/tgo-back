from django.urls import path

from .views import AnalyticsMaintenanceView, MerchantAnalyticsView, MetaConsentView, MetaDeliveryRetryView, MetaIntegrationTestView, MetaIntegrationView, StorefrontEventCreateView


urlpatterns = [
    path("store/<int:store_id>/analytics/", MerchantAnalyticsView.as_view(), name="merchant-analytics"),
    path("analytics/events/", StorefrontEventCreateView.as_view(), name="storefront-analytics-event"),
    path("analytics/meta-consent/", MetaConsentView.as_view(), name="meta-consent"),
    path("store/<int:store_id>/integrations/meta/", MetaIntegrationView.as_view(), name="meta-integration"),
    path("store/<int:store_id>/integrations/meta/test/", MetaIntegrationTestView.as_view(), name="meta-integration-test"),
    path("internal/analytics-maintenance/", AnalyticsMaintenanceView.as_view(), name="analytics-maintenance"),
    path("internal/meta-delivery-retry/", MetaDeliveryRetryView.as_view(), name="meta-delivery-retry"),
]
