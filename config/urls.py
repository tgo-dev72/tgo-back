from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

from catalog.views import ProductParametersView
from store.domain_internal_views import DomainQueueView
from platform_admin.views import PlatformMonitoringCronView
from store.public_seo_views import StorefrontMetadataView, StorefrontRobotsView, StorefrontSitemapView

urlpatterns = [
    path('api/public/storefront/<slug:slug>/robots.txt', StorefrontRobotsView.as_view(), name='storefront-robots'),
    path('api/public/storefront/<slug:slug>/sitemap.xml', StorefrontSitemapView.as_view(), name='storefront-sitemap'),
    path('api/public/storefront/<slug:slug>/seo', StorefrontMetadataView.as_view(), name='storefront-seo-metadata'),
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('api/', include('api.urls')),
    path('api/theme-previews/', include('theme_previewer.urls')),
    path('api/store/', include('store_mail.urls')),
    path('api/internal/mail/', include('store_mail.webhook_urls')),
    path('db/products/parameters/get', ProductParametersView.as_view()),
    path('api/internal/storefront-domains/execute', DomainQueueView.as_view()),
    path('api/internal/platform-monitoring/', PlatformMonitoringCronView.as_view(), name='platform-monitoring-cron'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Media files ship in the Vercel function bundle (git-tracked), so serve them
# in production too. MEDIA_ROOT paths are plain repo files — safe on serverless.
if not settings.DEBUG:
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
    ]
