from django.urls import path

from .views import (
    RequestDetailView,
    RequestEventsView,
    RequestListCreateView,
    RequestScaffoldView,
)

urlpatterns = [
    path("", RequestListCreateView.as_view()),
    path("<int:pk>/", RequestDetailView.as_view()),
    path("<int:pk>/events", RequestEventsView.as_view()),
    path("<int:pk>/scaffold", RequestScaffoldView.as_view()),
]
