from django.urls import include, path

from .storefront_config import StorefrontConfigView

urlpatterns = [
    path('store/', include('store.urls')),
    path('access/', include('access_control.urls')),
    path('catalog/', include('catalog.urls')),
    path('', include('coupons.urls')),
    path('storefront/config/', StorefrontConfigView.as_view()),
    path('', include('orders.urls')),
    path('', include('payments.urls')),
    path('', include('marketing.urls')),
    path('designs/', include('api.designs_urls')),
    path('requests/', include('api.requests_urls')),
    path('internal/storefront-deployments/', include('storefront_factory.internal_urls')),
    path('auth/', include('api.auth_urls')),
    path('platform-admin/', include('platform_admin.urls')),
    path('subscriptions/', include('subscriptions.urls')),
    path('', include('commerce_analytics.urls')),
]
