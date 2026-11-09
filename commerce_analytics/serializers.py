import json
import re
from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import StorefrontEvent


ALLOWED_EVENT_PROPERTIES = {
    "path", "query", "quantity", "cart_value", "currency", "source",
    "campaign", "referrer_host", "results_count",
}

LEGACY_EVENT_TYPES = {"purchase": StorefrontEvent.EventType.ORDER_SUBMITTED}
SENSITIVE_SEARCH_PATTERN = re.compile(r"(?:[^\s@]+@[^\s@]+\.[^\s@]+)|(?:\+?\d[\d\s().-]{6,}\d)")


class StorefrontEventSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    event_type = serializers.ChoiceField(
        choices=[
            *StorefrontEvent.EventType.choices,
            *((legacy, legacy) for legacy in LEGACY_EVENT_TYPES),
        ],
    )
    visitor_id = serializers.CharField(max_length=128, required=False, allow_blank=True, write_only=True)
    session_id = serializers.CharField(max_length=128, required=False, allow_blank=True, write_only=True)
    product_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    variant_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    order_id = serializers.UUIDField(required=False, allow_null=True)
    properties = serializers.JSONField(required=False, default=dict)
    occurred_at = serializers.DateTimeField(required=False, default=timezone.now)

    def validate_properties(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Event properties must be an object.")
        unknown = sorted(set(value) - ALLOWED_EVENT_PROPERTIES)
        if unknown:
            raise serializers.ValidationError(
                f"Unsupported event properties: {', '.join(unknown)}. Do not send personal data."
            )
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False)) > 2048:
            raise serializers.ValidationError("Event properties are too large.")
        normalized = {}
        for key, item in value.items():
            if not isinstance(item, (str, int, float)) or isinstance(item, bool):
                raise serializers.ValidationError(f"Property '{key}' must be text or a number.")
            if isinstance(item, str):
                item = item.strip()
                limit = 500 if key == "path" else 200
                if len(item) > limit:
                    raise serializers.ValidationError(f"Property '{key}' is too long.")
                if key == "path":
                    item = item.split("?", 1)[0].split("#", 1)[0]
                    if not item.startswith("/"):
                        raise serializers.ValidationError("Path must be relative to the storefront.")
                if key == "currency" and len(item) > 8:
                    raise serializers.ValidationError("Currency is invalid.")
                if key == "query":
                    item = " ".join(item.split())[:100]
                    if SENSITIVE_SEARCH_PATTERN.search(item):
                        raise serializers.ValidationError("Search analytics cannot contain contact details.")
                if key in {"source", "campaign", "referrer_host"}:
                    item = " ".join(item.split())[:100]
                    if SENSITIVE_SEARCH_PATTERN.search(item):
                        raise serializers.ValidationError(
                            f"Property '{key}' cannot contain contact details."
                        )
            if key == "results_count" and (not isinstance(item, int) or item < 0):
                raise serializers.ValidationError("Result count must be a non-negative whole number.")
            if key == "quantity" and (not isinstance(item, int) or item < 1):
                raise serializers.ValidationError("Quantity must be a positive whole number.")
            if key == "cart_value" and (
                not isinstance(item, (int, float)) or isinstance(item, bool) or item < 0
            ):
                raise serializers.ValidationError("Cart value cannot be negative.")
            normalized[key] = item
        return normalized

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["event_type"] = LEGACY_EVENT_TYPES.get(attrs["event_type"], attrs["event_type"])
        event_type = attrs["event_type"]
        properties = attrs.get("properties", {})
        if event_type in {
            StorefrontEvent.EventType.PRODUCT_VIEW,
            StorefrontEvent.EventType.ADD_TO_CART,
        } and not attrs.get("product_id"):
            raise serializers.ValidationError({"product_id": "This event requires a product."})
        if event_type == StorefrontEvent.EventType.ORDER_SUBMITTED and not attrs.get("order_id"):
            raise serializers.ValidationError({"order_id": "An order submission requires its order reference."})
        if event_type == StorefrontEvent.EventType.SEARCH:
            if not properties.get("query"):
                raise serializers.ValidationError({"properties": "Search events require a non-empty query."})
            if "results_count" not in properties:
                raise serializers.ValidationError({"properties": "Search events require a result count."})
        return attrs

    def validate_occurred_at(self, value):
        now = timezone.now()
        if value < now - timedelta(days=7) or value > now + timedelta(minutes=5):
            raise serializers.ValidationError("Event time is outside the accepted window.")
        return value
