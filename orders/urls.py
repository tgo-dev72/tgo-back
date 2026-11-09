from django.urls import path

from .views import (
    CustomerOrderListView,
    CustomerInvoiceDownloadView,
    MerchantOrderActionView,
    MerchantOrderDetailView,
    MerchantOrderListView,
    MerchantInvoiceDownloadView,
    MerchantInvoicePreviewView,
    PublicOrderCancelView,
    PublicOrderCreateView,
    PublicOrderDetailView,
    PublicOrderQuoteView,
)

urlpatterns = [
    path("orders/", PublicOrderCreateView.as_view(), name="order-create"),
    path("orders/quote/", PublicOrderQuoteView.as_view(), name="order-quote"),
    path("orders/mine/", CustomerOrderListView.as_view(), name="customer-orders"),
    path("orders/<uuid:order_id>/", PublicOrderDetailView.as_view(), name="order-detail"),
    path("orders/<uuid:order_id>/cancel/", PublicOrderCancelView.as_view(), name="order-cancel"),
    path("orders/<uuid:order_id>/invoice/", CustomerInvoiceDownloadView.as_view(), name="customer-invoice-download"),
    path("storefront/<slug:store_slug>/orders/<uuid:order_id>/invoice/", CustomerInvoiceDownloadView.as_view(), name="guest-invoice-download"),
    path("store/<int:store_id>/orders/", MerchantOrderListView.as_view(), name="merchant-orders"),
    path("store/<int:store_id>/orders/<uuid:order_id>/", MerchantOrderDetailView.as_view(), name="merchant-order-detail"),
    path("store/<int:store_id>/orders/<uuid:order_id>/invoice/", MerchantInvoiceDownloadView.as_view(), name="merchant-invoice-download"),
    path("store/<int:store_id>/invoice-preview/", MerchantInvoicePreviewView.as_view(), name="merchant-invoice-preview"),
    path("store/<int:store_id>/orders/<str:action>/", MerchantOrderActionView.as_view(), name="order-action"),
]
