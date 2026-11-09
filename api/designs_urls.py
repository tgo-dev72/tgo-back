from django.urls import path

from .views import DesignDetailView, DesignListView

urlpatterns = [
    path("", DesignListView.as_view()),
    path("<str:key>/", DesignDetailView.as_view()),
]
