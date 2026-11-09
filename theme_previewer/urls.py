from django.urls import path

from .views import ThemePreviewDetailView, ThemePreviewListView


urlpatterns = [
    path("", ThemePreviewListView.as_view(), name="theme-preview-list"),
    path("<slug:theme_key>/", ThemePreviewDetailView.as_view(), name="theme-preview-detail"),
]
