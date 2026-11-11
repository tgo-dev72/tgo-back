"""Merchant (dashboard) catalog management — full CRUD for sections,
attribute defs, products, variants and product images. Every view enforces
store membership through `_merchant_store()`."""
import csv
import io
import json

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils.text import slugify
from openpyxl import load_workbook
from rest_framework import generics, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError as APIValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from access_control.services import has_permission, membership_for
from store.models import Store, StoreMember
from common.media_uploads import image_folder, signed_upload_payload

from .models import (
    CatalogSection, Product, ProductImage, ProductReview, ProductVendor, RetailType, StockVariant, VariantSelection,
    attribute_key_in_use,
)
from .serializers import (
    MerchantAttributeDefSerializer,
    MerchantProductSerializer,
    MerchantProductReviewSerializer,
    MerchantProductVendorSerializer,
    MerchantRetailTypeSerializer,
    MerchantSectionSerializer,
    MerchantVariantSerializer,
    ProductImageReadSerializer,
    ProductImageWriteSerializer,
)
from .variant_services import archive_variant


def _merchant_store(user, store_id):
    store = generics.get_object_or_404(Store.objects.filter(members__user=user), pk=store_id)
    if membership_for(user, store) is None:
        raise PermissionDenied("This store membership is inactive.")
    return store


class CatalogAccessMixin:
    permission_map = {"GET": "products.view", "POST": "products.create", "PUT": "products.update", "PATCH": "products.update", "DELETE": "products.delete"}

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        store = _merchant_store(request.user, kwargs["store_id"])
        if not has_permission(request.user, store, self.permission_map.get(request.method, "products.view")):
            raise Http404


def _merchant_product(user, store, product_id):
    return generics.get_object_or_404(
        Product.objects.filter(store=store),
        pk=product_id,
    )


def _save_with_conflict_guidance(serializer, message, **kwargs):
    """Translate a last-millisecond uniqueness race into a safe API error."""
    try:
        return serializer.save(**kwargs)
    except IntegrityError as exc:
        raise APIValidationError({"detail": message}) from exc


class MerchantRetailTypeListView(generics.ListAPIView):
    """GET /api/catalog/retail-types/ — all available retail types."""
    serializer_class = MerchantRetailTypeSerializer
    permission_classes = [IsAuthenticated]
    queryset = RetailType.objects.all()
    pagination_class = None

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        if not queryset.exists():
            return Response(
                {
                    "detail": "Store categories are temporarily unavailable.",
                    "code": "retail_types_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class MerchantSectionListCreateView(CatalogAccessMixin, generics.ListCreateAPIView):
    """GET/POST /api/catalog/store/<id>/sections/ — manage catalog sections."""
    serializer_class = MerchantSectionSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "collections.view", "POST": "collections.manage"}

    def get_queryset(self):
        self.store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return CatalogSection.objects.filter(store=self.store).prefetch_related("attribute_defs")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context

    def perform_create(self, serializer):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        serializer.save(store=store)


SECTION_IMPORT_FIELDS = {
    "key", "name_en", "name_fr", "name_ar", "icon", "sort_order", "is_visible",
}


def _section_import_rows(upload):
    extension = upload.name.rsplit(".", 1)[-1].lower() if "." in upload.name else ""
    raw = upload.read()
    if extension == "json":
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIValidationError({"file": "The JSON file is not valid UTF-8 JSON."}) from exc
        rows = payload.get("sections") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise APIValidationError({"file": "JSON must be an array of sections or an object with a sections array."})
        return rows
    if extension == "csv":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise APIValidationError({"file": "The CSV file must use UTF-8 encoding."}) from exc
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        try:
            return list(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect))
        except csv.Error as exc:
            raise APIValidationError({"file": "The CSV file is malformed."}) from exc
    if extension == "xlsx":
        try:
            workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            sheet = workbook.active
            values = sheet.iter_rows(values_only=True)
            headers = [str(value or "").strip() for value in next(values, ())]
            rows = [dict(zip(headers, row)) for row in values]
            workbook.close()
            return rows
        except Exception as exc:
            raise APIValidationError({"file": "The Excel workbook could not be read."}) from exc
    raise APIValidationError({"file": "Choose a .xlsx, .csv, or .json file."})


def _import_bool(value, default=True):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "oui", "نعم"}:
        return True
    if normalized in {"false", "0", "no", "n", "non", "لا"}:
        return False
    raise ValueError("Use true/false, yes/no, or 1/0.")


class MerchantSectionImportView(CatalogAccessMixin, APIView):
    """Create or update sections by key from an XLSX, CSV, or JSON file."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    permission_map = {"POST": "collections.manage"}

    def post(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"file": "Choose a file to import."}, status=status.HTTP_400_BAD_REQUEST)
        if upload.size > 5 * 1024 * 1024:
            return Response({"file": "The import file must be 5 MB or smaller."}, status=status.HTTP_400_BAD_REQUEST)
        rows = [
            row for row in _section_import_rows(upload)
            if any(value not in (None, "") for value in row.values())
        ]
        if not rows:
            return Response({"file": "The import file has no section rows."}, status=status.HTTP_400_BAD_REQUEST)
        if len(rows) > 1000:
            return Response({"file": "Import at most 1,000 sections at a time."}, status=status.HTTP_400_BAD_REQUEST)

        errors = []
        prepared = []
        seen_keys = set()
        for index, source in enumerate(rows, start=2):
            row = {str(key).strip().lower(): value for key, value in source.items() if key is not None}
            unknown = sorted(set(row) - SECTION_IMPORT_FIELDS)
            if unknown:
                errors.append({"row": index, "field": unknown[0], "message": "Unknown column."})
                continue
            translations = {
                language: str(row.get(f"name_{language}") or "").strip()
                for language in ("en", "fr", "ar")
            }
            available_name = translations["en"] or next((value for value in translations.values() if value), "")
            key = slugify(str(row.get("key") or available_name))
            if not available_name:
                errors.append({"row": index, "field": "name_en", "message": "Add a name in at least one language."})
                continue
            if not key:
                errors.append({"row": index, "field": "key", "message": "Add a valid key or English name."})
                continue
            if key in seen_keys:
                errors.append({"row": index, "field": "key", "message": "This key appears more than once in the file."})
                continue
            seen_keys.add(key)
            try:
                sort_order = int(row.get("sort_order") or 0)
                if sort_order < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append({"row": index, "field": "sort_order", "message": "Use a whole number of 0 or greater."})
                continue
            try:
                is_visible = _import_bool(row.get("is_visible"))
            except ValueError as exc:
                errors.append({"row": index, "field": "is_visible", "message": str(exc)})
                continue
            prepared.append((key, {
                "key": key,
                "label": available_name,
                "label_translations": translations,
                "icon": str(row.get("icon") or "FaBoxOpen:fa").strip(),
                "sort_order": sort_order,
                "is_visible": is_visible,
            }, index))
        if errors:
            return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)

        created = updated = 0
        with transaction.atomic():
            for key, data, row_number in prepared:
                instance = CatalogSection.objects.filter(store=store, key=key).first()
                serializer = MerchantSectionSerializer(
                    instance, data=data, partial=instance is not None,
                    context={"store": store, "request": request},
                )
                if not serializer.is_valid():
                    transaction.set_rollback(True)
                    return Response(
                        {"errors": [{"row": row_number, "field": field, "message": str(messages[0])} for field, messages in serializer.errors.items()]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                serializer.save(store=store) if instance is None else serializer.save()
                created += instance is None
                updated += instance is not None
        return Response({"created": created, "updated": updated, "total": created + updated})


class MerchantSectionRetrieveUpdateDestroyView(CatalogAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/catalog/store/<id>/sections/<pk>/"""
    serializer_class = MerchantSectionSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "collections.view", "PUT": "collections.manage", "PATCH": "collections.manage", "DELETE": "collections.manage"}

    def get_queryset(self):
        self.store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return CatalogSection.objects.filter(store=self.store).prefetch_related("attribute_defs")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.products.exists():
            return Response(
                {"detail": "Section still has products — move or delete them first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MerchantProductListCreateView(CatalogAccessMixin, generics.ListCreateAPIView):
    """GET/POST /api/catalog/store/<id>/products/ — merchant catalog (incl. inactive)."""
    serializer_class = MerchantProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        self.store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return (
            Product.objects.filter(store=self.store)
            .select_related("section")
            .prefetch_related("images", "variants__image", "categories__attribute_defs", "vendors", "related_products")
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context

    @transaction.atomic
    def perform_create(self, serializer):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        from store.models import StoreMember
        from subscriptions.policies import EntitlementDenied, enforce_operation

        owner = store.members.select_related("user").get(role=StoreMember.Roles.OWNER).user
        # Product quotas are store-scoped. Lock both the subscription owner and
        # the store so simultaneous requests from different team members cannot
        # all pass the same count-before-insert check.
        get_user_model().objects.select_for_update().only("pk").get(pk=owner.pk)
        type(store).objects.select_for_update().only("pk").get(pk=store.pk)
        try:
            enforce_operation(owner, "catalog.products.create", context={"store": store})
        except EntitlementDenied as exc:
            raise PermissionDenied(exc.message) from exc
        _save_with_conflict_guidance(
            serializer,
            "A product with this reference or address already exists.",
            store=store,
        )


class MerchantProductBulkDestroyView(CatalogAccessMixin, APIView):
    """Delete the complete store catalog after explicit owner confirmation."""
    permission_classes = [IsAuthenticated]
    permission_map = {"DELETE": "products.delete"}

    @transaction.atomic
    def delete(self, request, store_id):
        store = _merchant_store(request.user, store_id)
        membership = membership_for(request.user, store)
        if membership is None or membership.role != StoreMember.Roles.OWNER:
            raise Http404
        if request.data.get("store_slug") != store.slug:
            return Response({"store_slug": "Enter the exact store address."}, status=status.HTTP_400_BAD_REQUEST)
        if request.data.get("confirmation") != "delete all products":
            return Response({"confirmation": "Enter the exact confirmation phrase."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            expected_count = int(request.data.get("expected_count"))
        except (TypeError, ValueError):
            return Response({"expected_count": "Confirm the current product count."}, status=status.HTTP_400_BAD_REQUEST)
        type(store).objects.select_for_update().only("pk").get(pk=store.pk)
        products = Product.objects.select_for_update().filter(store=store)
        current_count = products.count()
        if expected_count != current_count:
            return Response(
                {"code": "catalog_changed", "detail": "The catalog changed. Review the new product count and confirm again.", "count": current_count},
                status=status.HTTP_409_CONFLICT,
            )
        VariantSelection.objects.filter(variant__product__store=store).delete()
        products.delete()
        return Response({"deleted": current_count}, status=status.HTTP_200_OK)


class MerchantProductRetrieveUpdateDestroyView(CatalogAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/catalog/store/<id>/products/<pk>/"""
    serializer_class = MerchantProductSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        self.store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return (
            Product.objects.filter(store=self.store)
            .select_related("section")
            .prefetch_related("images", "variants__image", "categories__attribute_defs", "vendors", "related_products")
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context

    def perform_update(self, serializer):
        _save_with_conflict_guidance(
            serializer,
            "A product with this reference or address already exists.",
        )


class MerchantProductImageCreateView(CatalogAccessMixin, generics.CreateAPIView):
    """POST /api/catalog/store/<id>/products/<pk>/images/ — multipart upload."""
    serializer_class = ProductImageWriteSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    permission_map = {"POST": "products.update"}

    def get_serializer_class(self):
        if self.request.method == "POST":
            return ProductImageWriteSerializer
        return ProductImageReadSerializer

    def perform_create(self, serializer):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        if serializer.validated_data.get("is_primary"):
            product.images.filter(is_primary=True).update(is_primary=False)
        serializer.save(product=product)

    def create(self, request, *args, **kwargs):
        store = _merchant_store(request.user, self.kwargs["store_id"])
        product = _merchant_product(request.user, store, self.kwargs["pk"])
        write_serializer = self.get_serializer(
            data=request.data,
            context={"request": request, "store": store, "product": product},
        )
        write_serializer.is_valid(raise_exception=True)
        self.perform_create(write_serializer)
        instance = write_serializer.instance
        read_serializer = ProductImageReadSerializer(instance, context={"request": request})
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)


class MerchantProductImageSignView(CatalogAccessMixin, APIView):
    permission_classes = [IsAuthenticated]
    permission_map = {"POST": "products.update"}

    def post(self, request, store_id, pk):
        store = _merchant_store(request.user, store_id)
        product = _merchant_product(request.user, store, pk)
        return Response(signed_upload_payload(image_folder(store.id, "products", product.id)))


class MerchantProductImageDestroyView(CatalogAccessMixin, generics.DestroyAPIView):
    """DELETE /api/catalog/store/<id>/products/<pk>/images/<image_pk>/"""
    permission_classes = [IsAuthenticated]
    lookup_url_kwarg = "image_pk"

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        return ProductImage.objects.filter(product=product)


class MerchantVariantListCreateView(CatalogAccessMixin, generics.ListCreateAPIView):
    """GET/POST /api/catalog/store/<id>/products/<pk>/variants/"""
    serializer_class = MerchantVariantSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "inventory.view", "POST": "inventory.update"}

    def get_serializer_context(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        return {"store": store, "product": product}

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        return StockVariant.objects.filter(store=store, product=product)

    def perform_create(self, serializer):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        _save_with_conflict_guidance(
            serializer,
            "This SKU, barcode, or option combination is already in use.",
            store=store,
            product=product,
        )


class MerchantVariantRetrieveUpdateDestroyView(CatalogAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/catalog/store/<id>/products/<pk>/variants/<variant_pk>/"""
    serializer_class = MerchantVariantSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "inventory.view", "PUT": "inventory.update", "PATCH": "inventory.update", "DELETE": "inventory.update"}
    lookup_url_kwarg = "variant_pk"

    def get_serializer_context(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        return {"store": store, "product": product}

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        product = _merchant_product(self.request.user, store, self.kwargs["pk"])
        return StockVariant.objects.filter(store=store, product=product)

    def destroy(self, request, *args, **kwargs):
        variant = self.get_object()
        if variant.product.is_active and not variant.product.variants.exclude(pk=variant.pk).exists():
            return Response(
                {"detail": "Deactivate this product before removing its last inventory variant."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        archive_variant(variant)
        return Response({"status": "archived", "id": variant.pk}, status=status.HTTP_200_OK)


class MerchantAttributeDefListCreateView(CatalogAccessMixin, generics.ListCreateAPIView):
    """GET/POST /api/catalog/store/<id>/sections/<section_pk>/attributes/"""
    serializer_class = MerchantAttributeDefSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "collections.view", "POST": "collections.manage"}

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        section = generics.get_object_or_404(
            CatalogSection.objects.filter(store=store),
            pk=self.kwargs["section_pk"],
        )
        return section.attribute_defs.all()

    def perform_create(self, serializer):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        section = generics.get_object_or_404(
            CatalogSection.objects.filter(store=store),
            pk=self.kwargs["section_pk"],
        )
        serializer.save(store=store, section=section)


class MerchantAttributeDefRetrieveUpdateDestroyView(CatalogAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/catalog/store/<id>/sections/<section_pk>/attributes/<attr_pk>/"""
    serializer_class = MerchantAttributeDefSerializer
    permission_classes = [IsAuthenticated]
    permission_map = {"GET": "collections.view", "PUT": "collections.manage", "PATCH": "collections.manage", "DELETE": "collections.manage"}
    lookup_url_kwarg = "attr_pk"

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        section = generics.get_object_or_404(
            CatalogSection.objects.filter(store=store),
            pk=self.kwargs["section_pk"],
        )
        return section.attribute_defs.all()

    def destroy(self, request, *args, **kwargs):
        definition = self.get_object()
        if attribute_key_in_use(definition.section, definition.key):
            return Response(
                {"detail": "This attribute is still used by catalog records. Clean those values first."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        self.perform_destroy(definition)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MerchantProductVendorListCreateView(CatalogAccessMixin, generics.ListCreateAPIView):
    serializer_class = MerchantProductVendorSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return ProductVendor.objects.filter(store=store)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context

    def perform_create(self, serializer):
        _save_with_conflict_guidance(
            serializer,
            "A vendor with this address already exists.",
            store=_merchant_store(self.request.user, self.kwargs["store_id"]),
        )


class MerchantProductVendorDetailView(CatalogAccessMixin, generics.RetrieveUpdateDestroyAPIView):
    serializer_class = MerchantProductVendorSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return ProductVendor.objects.filter(store=store)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = _merchant_store(self.request.user, self.kwargs["store_id"])
        return context


class MerchantProductReviewListView(CatalogAccessMixin, generics.ListAPIView):
    serializer_class = MerchantProductReviewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return ProductReview.objects.filter(store=store).select_related("product")


class MerchantProductReviewModerateView(CatalogAccessMixin, generics.RetrieveUpdateAPIView):
    serializer_class = MerchantProductReviewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        store = _merchant_store(self.request.user, self.kwargs["store_id"])
        return ProductReview.objects.filter(store=store).select_related("product")
