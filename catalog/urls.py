from django.urls import path
from .configuration import MerchantCatalogConfigurationView

from .merchant_views import (
    MerchantAttributeDefListCreateView,
    MerchantAttributeDefRetrieveUpdateDestroyView,
    MerchantProductImageCreateView,
    MerchantProductImageSignView,
    MerchantProductImageDestroyView,
    MerchantProductBulkDestroyView,
    MerchantProductListCreateView,
    MerchantProductReviewListView,
    MerchantProductReviewModerateView,
    MerchantProductRetrieveUpdateDestroyView,
    MerchantProductVendorDetailView,
    MerchantProductVendorListCreateView,
    MerchantRetailTypeListView,
    MerchantSectionListCreateView,
    MerchantSectionRetrieveUpdateDestroyView,
    MerchantVariantListCreateView,
    MerchantVariantRetrieveUpdateDestroyView,
)
from .views import (
    CatalogSectionListView, ProductDetailView, ProductListView,
    ProductReviewListCreateView, StockVariantListView,
)
from .transfers import (
    CatalogExportView, CatalogImportCommitView, CatalogImportGuideView,
    CatalogImportTemplateView, CatalogImportValidateView, CatalogTransferCapabilitiesView,
)

urlpatterns = [
    path("sections/", CatalogSectionListView.as_view()),
    path("products/", ProductListView.as_view()),
    path("products/<slug:slug>/", ProductDetailView.as_view()),
    path("products/<slug:slug>/reviews/", ProductReviewListCreateView.as_view()),
    path("products/<int:product_id>/stock/", StockVariantListView.as_view()),
    # ── Retail types ───────────────────────────────────────────────────
    path("retail-types/", MerchantRetailTypeListView.as_view()),
    # ── Merchant (dashboard) catalog management ──────────────────────
    path("store/<int:store_id>/configuration/", MerchantCatalogConfigurationView.as_view()),
    path("store/<int:store_id>/sections/", MerchantSectionListCreateView.as_view()),
    path("store/<int:store_id>/sections/<int:pk>/", MerchantSectionRetrieveUpdateDestroyView.as_view()),
    path(
        "store/<int:store_id>/sections/<int:section_pk>/attributes/",
        MerchantAttributeDefListCreateView.as_view(),
    ),
    path(
        "store/<int:store_id>/sections/<int:section_pk>/attributes/<int:attr_pk>/",
        MerchantAttributeDefRetrieveUpdateDestroyView.as_view(),
    ),
    path("store/<int:store_id>/products/", MerchantProductListCreateView.as_view()),
    path("store/<int:store_id>/products/delete-all/", MerchantProductBulkDestroyView.as_view()),
    path("store/<int:store_id>/export.csv", CatalogExportView.as_view(), name="catalog-export"),
    path("store/<int:store_id>/transfer-capabilities/", CatalogTransferCapabilitiesView.as_view(), name="catalog-transfer-capabilities"),
    path("store/<int:store_id>/import-guide/", CatalogImportGuideView.as_view(), name="catalog-import-guide"),
    path("store/<int:store_id>/import-template/", CatalogImportTemplateView.as_view(), name="catalog-import-template"),
    path("store/<int:store_id>/imports/validate/", CatalogImportValidateView.as_view(), name="catalog-import-validate"),
    path("store/<int:store_id>/imports/<uuid:operation_id>/commit/", CatalogImportCommitView.as_view(), name="catalog-import-commit"),
    path("store/<int:store_id>/products/<int:pk>/", MerchantProductRetrieveUpdateDestroyView.as_view()),
    path("store/<int:store_id>/vendors/", MerchantProductVendorListCreateView.as_view()),
    path("store/<int:store_id>/vendors/<int:pk>/", MerchantProductVendorDetailView.as_view()),
    path("store/<int:store_id>/reviews/", MerchantProductReviewListView.as_view()),
    path("store/<int:store_id>/reviews/<int:pk>/", MerchantProductReviewModerateView.as_view()),
    path("store/<int:store_id>/products/<int:pk>/images/", MerchantProductImageCreateView.as_view()),
    path("store/<int:store_id>/products/<int:pk>/images/sign/", MerchantProductImageSignView.as_view()),
    path(
        "store/<int:store_id>/products/<int:pk>/images/<int:image_pk>/",
        MerchantProductImageDestroyView.as_view(),
    ),
    path("store/<int:store_id>/products/<int:pk>/variants/", MerchantVariantListCreateView.as_view()),
    path(
        "store/<int:store_id>/products/<int:pk>/variants/<int:variant_pk>/",
        MerchantVariantRetrieveUpdateDestroyView.as_view(),
    ),
]
