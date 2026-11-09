"""Store-owned operational fields; presets never rewrite a live catalog."""
from copy import deepcopy

from django.core.exceptions import ValidationError as ModelValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from store.access import MANAGER_ROLES, member_stores
from store.models import Store
from .models import Product, RetailType, StockVariant, validate_extra_fields
from .retail_presets import EXTRA_FIELD_CHOICES, EXTRA_FIELD_TYPES


class CatalogConfigurationInput(serializers.Serializer):
    retail_type = serializers.PrimaryKeyRelatedField(queryset=RetailType.objects.all(), required=False, allow_null=True)
    product_fields = serializers.DictField(required=False)
    variant_fields = serializers.DictField(required=False)

    def validate(self, attrs):
        for group in ("product_fields", "variant_fields"):
            for key, supplied in attrs.get(group, {}).items():
                if key not in EXTRA_FIELD_TYPES or not isinstance(supplied, dict):
                    raise serializers.ValidationError({group: "Choose a supported operational field."})
                if set(supplied) - {"enabled", "visible", "required", "sort_order"}:
                    raise serializers.ValidationError({group: "Operational field types and allowed values cannot be changed."})
                for flag in ("enabled", "visible", "required"):
                    if flag in supplied and not isinstance(supplied[flag], bool):
                        raise serializers.ValidationError({group: f"{key}.{flag} must be true or false."})
                order = supplied.get("sort_order", 0)
                if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 1000:
                    raise serializers.ValidationError({group: "Display order must be a whole number from 0 to 1000."})
        return attrs


def representation(store):
    return {
        "retail_type": store.retail_type_id,
        "catalog_config": store.catalog_config,
        "registry": [{"key": key, "type": kind, "choices": EXTRA_FIELD_CHOICES.get(key, [])} for key, kind in EXTRA_FIELD_TYPES.items()],
    }


class MerchantCatalogConfigurationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, store_id):
        store = get_object_or_404(member_stores(request.user, MANAGER_ROLES), pk=store_id)
        return Response(representation(store))

    @transaction.atomic
    def patch(self, request, store_id):
        allowed = get_object_or_404(member_stores(request.user, MANAGER_ROLES), pk=store_id)
        store = Store.objects.select_for_update().get(pk=allowed.pk)
        serializer = CatalogConfigurationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        config = deepcopy(store.catalog_config)
        for group in ("product_fields", "variant_fields"):
            if not data.get(group):
                continue
            fields = config.setdefault(group, {})
            for key, changes in data[group].items():
                field = deepcopy(fields.get(key, {"enabled": False, "visible": False, "required": False}))
                field.update(changes)
                field["type"] = EXTRA_FIELD_TYPES[key]
                if key in EXTRA_FIELD_CHOICES:
                    field["choices"] = deepcopy(EXTRA_FIELD_CHOICES[key])
                if not field.get("enabled", True):
                    field.update(visible=False, required=False)
                if field.get("required") and not field.get("visible"):
                    raise serializers.ValidationError({group: "Required fields must be visible in the editor."})
                fields[key] = field
            # Reject changes which would invalidate stored data. Hiding a field
            # preserves its values; disabling one in use requires moving them first.
            model = Product if group == "product_fields" else StockVariant
            for extra in model.objects.filter(store=store).values_list("extra", flat=True).iterator():
                try:
                    validate_extra_fields(extra, store.retail_type, config=fields)
                except ModelValidationError as exc:
                    raise serializers.ValidationError({
                        "code": "catalog_fields_in_use",
                        "detail": "These settings would invalidate existing product or stock values. Keep fields in use enabled and fill missing values before making a field required.",
                    }) from exc
        store.catalog_config = config
        if "retail_type" in data:
            store.retail_type = data["retail_type"]
        store.save(update_fields=["catalog_config", "retail_type"])
        return Response(representation(store))
