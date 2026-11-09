from django.urls import path

from .views import CouponDetailView, CouponListCreateView


urlpatterns = [
    path("store/<int:store_id>/coupons/", CouponListCreateView.as_view()),
    path("store/<int:store_id>/coupons/<int:pk>/", CouponDetailView.as_view()),
]

