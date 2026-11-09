from rest_framework import serializers

from catalog.models import CatalogSection, Product

from .models import Coupon


class CouponSerializer(serializers.ModelSerializer):
    category_ids = serializers.PrimaryKeyRelatedField(
        source="categories",
        many=True,
        queryset=CatalogSection.objects.none(),
        required=False,
    )
    product_ids = serializers.PrimaryKeyRelatedField(
        source="products",
        many=True,
        queryset=Product.objects.none(),
        required=False,
    )
    availability = serializers.CharField(read_only=True)

    class Meta:
        model = Coupon
        fields = [
            "id",
            "code",
            "discount_type",
            "value",
            "scope",
            "category_ids",
            "product_ids",
            "minimum_subtotal",
            "maximum_discount",
            "total_usage_limit",
            "per_customer_usage_limit",
            "usage_count",
            "starts_at",
            "ends_at",
            "is_active",
            "availability",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "usage_count", "availability", "created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        store = self.context.get("store")
        if store is not None:
            self.fields["category_ids"].child_relation.queryset = CatalogSection.objects.filter(store=store)
            self.fields["product_ids"].child_relation.queryset = Product.objects.filter(store=store)

    def validate_code(self, value):
        return value.strip().upper()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        instance = self.instance
        store = self.context.get("store")
        code = attrs.get("code", getattr(instance, "code", ""))
        duplicates = Coupon.objects.filter(store=store, code__iexact=code)
        if instance is not None:
            duplicates = duplicates.exclude(pk=instance.pk)
        if store is not None and duplicates.exists():
            raise serializers.ValidationError({"code": "This code is already used in this store."})
        discount_type = attrs.get("discount_type", getattr(instance, "discount_type", None))
        value = attrs.get("value", getattr(instance, "value", None))
        maximum = attrs.get("maximum_discount", getattr(instance, "maximum_discount", None))
        starts_at = attrs.get("starts_at", getattr(instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(instance, "ends_at", None))
        scope = attrs.get("scope", getattr(instance, "scope", Coupon.Scope.ALL_PRODUCTS))

        if discount_type == Coupon.DiscountType.PERCENTAGE and value is not None and value > 100:
            raise serializers.ValidationError({"value": "A percentage discount cannot exceed 100%."})
        if discount_type == Coupon.DiscountType.FIXED and maximum is not None:
            raise serializers.ValidationError({"maximum_discount": "Leave this empty for a fixed discount."})
        if starts_at and ends_at and ends_at <= starts_at:
            raise serializers.ValidationError({"ends_at": "Choose an end later than the start."})

        categories = attrs.get("categories")
        products = attrs.get("products")
        current_categories = list(instance.categories.all()) if instance and categories is None else (categories or [])
        current_products = list(instance.products.all()) if instance and products is None else (products or [])
        if scope == Coupon.Scope.CATEGORIES and not current_categories:
            raise serializers.ValidationError({"category_ids": "Select at least one category."})
        if scope == Coupon.Scope.PRODUCTS and not current_products:
            raise serializers.ValidationError({"product_ids": "Select at least one product."})
        return attrs

    def _clear_irrelevant_scope(self, coupon):
        if coupon.scope != Coupon.Scope.CATEGORIES:
            coupon.categories.clear()
        if coupon.scope != Coupon.Scope.PRODUCTS:
            coupon.products.clear()
        return coupon

    def create(self, validated_data):
        return self._clear_irrelevant_scope(super().create(validated_data))

    def update(self, instance, validated_data):
        return self._clear_irrelevant_scope(super().update(instance, validated_data))
