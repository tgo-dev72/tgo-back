from django.db.models import Case, F, FloatField, Prefetch, Q, When
from django.db.models.functions import Cast, Round
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from store.context import resolve_store

from .models import CatalogSection, Product, ProductReview, StockVariant
from .serializers import (
    CatalogSectionSerializer,
    ProductReviewSerializer,
    ProductSerializer,
    StockVariantSerializer,
)

# Catalog data is read-mostly and identical across visitors for a given store.
# Cache it at the CDN edge so storefront catalog fetches are edge hits.
CACHE_CONTROL = "s-maxage=60, stale-while-revalidate=600"


class _CacheControlMixin:
    """Sets a CDN-friendly Cache-Control header on every response, keyed on the
    store slug so distinct stores never share a cached catalog."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = CACHE_CONTROL
        response["Vary"] = "X-Store-Slug"
        return response


class _StoreScopedView(_CacheControlMixin):
    """Sets `store` from the request (subdomain or X-Store-Slug header)."""

    def initial(self, request, *args, **kwargs):
        self.store = resolve_store(request)
        super().initial(request, *args, **kwargs)


class CatalogSectionListView(_StoreScopedView, generics.ListAPIView):
    """Public: the merchant-configured catalog sections + attribute schema."""
    serializer_class = CatalogSectionSerializer
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        return CatalogSection.objects.filter(store=self.store, is_visible=True).prefetch_related("attribute_defs")


class ProductListView(_StoreScopedView, generics.ListAPIView):
    """Public: products for a store, optionally filtered by section."""
    serializer_class = ProductSerializer
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = Product.objects.filter(store=self.store, is_active=True)
        section = self.request.query_params.get("section")
        if section:
            qs = qs.filter(Q(section__key=section) | Q(categories__key=section)).distinct()
        source = self.request.query_params.get("source")
        if source == "featured":
            qs = qs.filter(is_featured=True).order_by("merchandising_order", "-created_at", "-pk")
        elif source == "discounted":
            # Match the storefront's effective discount: compare-at price wins
            # over the legacy promo field when both are present.
            qs = qs.filter(Q(compare_at_price__gt=F("price")) | Q(promo__gt=0)).annotate(
                effective_discount=Case(
                    When(
                        compare_at_price__gt=F("price"),
                        then=Round(
                            100.0 * (1.0 - Cast(F("price"), FloatField()) / Cast(F("compare_at_price"), FloatField())),
                            0,
                        ),
                    ),
                    default=F("promo"),
                    output_field=FloatField(),
                ),
            ).order_by("-effective_discount", "-created_at", "-pk")
        elif source == "top_rated":
            qs = qs.filter(reviews_count__gt=0).order_by("-rating", "-reviews_count", "-pk")
        elif source == "newest":
            qs = qs.order_by("-created_at", "-pk")
        elif source in (None, ""):
            qs = qs.order_by("-is_featured", "merchandising_order", "-created_at", "-pk")
        else:
            raise ValidationError({"source": "Choose a supported product selection."})
        return qs.select_related("section").prefetch_related(
            "images", Prefetch("variants", queryset=StockVariant.objects.filter(is_active=True).select_related("image")), "categories__attribute_defs", "vendors", "related_products",
        )

    def list(self, request, *args, **kwargs):
        # Keep the historical array response for existing clients. New
        # storefronts opt into bounded pages with ?limit=&offset=.
        if "limit" not in request.query_params:
            return super().list(request, *args, **kwargs)
        try:
            limit = int(request.query_params["limit"])
            offset = int(request.query_params.get("offset", "0"))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"pagination": "Limit and offset must be integers."}) from exc
        if not 1 <= limit <= 48 or offset < 0:
            raise ValidationError({"pagination": "Limit must be 1–48 and offset cannot be negative."})
        queryset = self.filter_queryset(self.get_queryset())
        count = queryset.count()
        products = queryset[offset:offset + limit]
        return Response({"count": count, "results": self.get_serializer(products, many=True).data})


class ProductDetailView(_StoreScopedView, generics.RetrieveAPIView):
    """Public product detail resolved by the stable store-scoped slug."""

    serializer_class = ProductSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    lookup_field = "slug"

    def get_queryset(self):
        return Product.objects.filter(store=self.store, is_active=True).select_related("section").prefetch_related(
            "images", Prefetch("variants", queryset=StockVariant.objects.filter(is_active=True).select_related("image")), "categories__attribute_defs", "vendors", "related_products",
        )


class ProductReviewListCreateView(_StoreScopedView, generics.ListCreateAPIView):
    """List published reviews and accept new reviews into moderation."""

    serializer_class = ProductReviewSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "product_review"

    def get_throttles(self):
        return super().get_throttles() if self.request.method == "POST" else []

    def get_product(self):
        return generics.get_object_or_404(
            Product.objects.filter(store=self.store, is_active=True),
            slug=self.kwargs["slug"],
        )

    def get_queryset(self):
        return ProductReview.objects.filter(
            store=self.store,
            product=self.get_product(),
            status=ProductReview.Status.PUBLISHED,
        )

    def perform_create(self, serializer):
        serializer.save(
            store=self.store,
            product=self.get_product(),
            status=ProductReview.Status.PENDING,
            verified_purchase=False,
        )


class StockVariantListView(_StoreScopedView, generics.ListAPIView):
    """Public: inventory for a product (attributes + availability)."""
    serializer_class = StockVariantSerializer
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_queryset(self):
        product_id = self.kwargs.get("product_id")
        return StockVariant.objects.filter(store=self.store, product_id=product_id, is_active=True)


class ProductParametersView(APIView):
    """Legacy endpoint: GET /db/products/parameters/get

    Returns the section keys as ``types`` and each section's first
    select-attribute options as ``categories`` — matching the shape the
    storefront frontend expects.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        store = resolve_store(request)
        sections = CatalogSection.objects.filter(store=store).prefetch_related("attribute_defs")

        types = [s.key for s in sections]
        categories: dict[str, list[str]] = {}
        for section in sections:
            attr = section.attribute_defs.filter(type__in=("select", "multi")).first()
            if attr and attr.options:
                categories[section.key] = list(attr.options)

        return Response({"types": types, "categories": categories})
