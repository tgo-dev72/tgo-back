from django.urls import path

from .internal_views import StorefrontDeploymentExecuteView, StorefrontDeploymentReconcileView


urlpatterns = [
    path("<int:pk>/execute", StorefrontDeploymentExecuteView.as_view()),
    path("<int:pk>/reconcile", StorefrontDeploymentReconcileView.as_view()),
]
