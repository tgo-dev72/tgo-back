"""Authoritative product-option, variant-combination and pricing services."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import Product, ProductOption, ProductOptionValue, StockVariant, VariantSelection


def stable_code(value: object, fallback: str) -> str:
    """Generate a language-independent persisted code once; labels may later change."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    code = slugify(text).replace("-", "_")[:50]
    return code or fallback


def option_projection(product: Product) -> list[dict]:
    result = []
    for option in product.options.prefetch_related("values").filter(is_active=True):
        values = []
        for value in option.values.filter(is_active=True):
            # Preserve the established storefront/dashboard JSON shape while
            # relational IDs are exposed through variant ``selections``.
            # Plain values must remain strings; metadata objects are reserved
            # for colors that actually define a swatch.
            values.append(
                {"value": value.code, "label": value.label, "swatch": value.swatch}
                if value.swatch else value.code
            )
        result.append({
            "id": option.id,
            "key": option.code,
            "label": option.label,
            "kind": option.kind,
            "type": "color" if option.kind == ProductOption.Kind.COLOR else "text" if option.kind == ProductOption.Kind.TEXT else "select",
            "presentation": option.presentation,
            "values": values,
        })
    return result


def combination_fingerprint(pairs: list[tuple[int, int]]) -> str:
    canonical = ";".join(f"{option_id}:{value_id}" for option_id, value_id in sorted(pairs))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


@transaction.atomic
def configure_product_options(product: Product, definitions: list[dict]) -> list[ProductOption]:
    """Create/update product-owned definitions and their allowed values.

    This is the only write path for relational options. The legacy product JSON
    is refreshed afterwards as a compatibility projection.
    """
    product = Product.objects.select_for_update().get(pk=product.pk)
    if len(definitions) > 3:
        raise ValidationError({"options": "A product supports at most three options."})
    retained = []
    seen_codes = set()
    for position, definition in enumerate(definitions):
        kind = definition.get("kind")
        if kind not in ProductOption.Kind.values:
            raise ValidationError({"kind": f"Unsupported option kind: {kind}."})
        code = str(definition.get("code") or "").strip() or stable_code(definition.get("name"), f"option_{position + 1}")
        if code in seen_codes:
            raise ValidationError({"options": f"Duplicate option identity: {code}."})
        seen_codes.add(code)
        option, _ = ProductOption.objects.update_or_create(
            product=product, code=code,
            defaults={
                "store": product.store, "label": str(definition.get("name") or code).strip(),
                "kind": kind, "presentation": definition.get("presentation") or (
                    ProductOption.Presentation.SWATCHES if kind == ProductOption.Kind.COLOR else ProductOption.Presentation.BUTTONS
                ),
                "position": position, "required": True, "is_active": True,
            },
        )
        retained.append(option)
        seen_values = set()
        retained_value_ids = []
        for value_position, raw in enumerate(definition.get("values") or []):
            item = raw if isinstance(raw, dict) else {"value": raw, "label": raw}
            raw_code = str(item.get("code", item.get("value", ""))).strip()
            if not raw_code:
                continue
            normalized = unicodedata.normalize("NFKC", raw_code).casefold()
            if normalized in seen_values:
                continue
            seen_values.add(normalized)
            existing = option.values.filter(code__iexact=raw_code).first()
            value = existing or ProductOptionValue(option=option, code=raw_code)
            value.label = str(item.get("label") or raw_code).strip()
            value.swatch = str(item.get("swatch") or "").strip().lower()
            value.position, value.is_active = value_position, True
            value.full_clean()
            value.save()
            retained_value_ids.append(value.pk)
        option.values.exclude(pk__in=retained_value_ids).update(is_active=False)
    product.options.exclude(pk__in=[item.pk for item in retained]).update(is_active=False)
    Product.objects.filter(pk=product.pk).update(variant_options=option_projection(product))
    return retained


@transaction.atomic
def set_variant_selections(variant: StockVariant, selections: list[tuple[ProductOption, ProductOptionValue]]) -> StockVariant:
    """Atomically validate selections, replace them and persist their fingerprint."""
    variant = StockVariant.objects.select_for_update().select_related("product").get(pk=variant.pk)
    active_options = list(variant.product.options.select_for_update().filter(is_active=True))
    by_option = {}
    attributes = {}
    for option, value in selections:
        if option.product_id != variant.product_id:
            raise ValidationError({"option": "Selected option does not belong to this product."})
        if value.option_id != option.id:
            raise ValidationError({"value": "Selected value does not belong to the selected option."})
        if option.id in by_option:
            raise ValidationError({"option": "Each required option must be selected once."})
        by_option[option.id] = value
        attributes[option.code] = value.code
    required = {item.id for item in active_options if item.required}
    if set(by_option) != required:
        raise ValidationError({"selections": "Select exactly one value for every required option."})
    pairs = [(option_id, value.id) for option_id, value in by_option.items()]
    key = combination_fingerprint(pairs) if pairs else hashlib.sha256(b"default").hexdigest()
    VariantSelection.objects.filter(variant=variant).delete()
    VariantSelection.objects.bulk_create([
        VariantSelection(variant=variant, option_id=option_id, value=value)
        for option_id, value in by_option.items()
    ])
    variant.attributes, variant.combination_key = attributes, key
    try:
        variant.save(update_fields=["attributes", "combination_key", "updated_at"])
    except IntegrityError as exc:
        raise ValidationError({"selections": "This option combination already exists."}) from exc
    return variant


def effective_variant_pricing(product: Product, variant: StockVariant) -> dict:
    selling = variant.price if variant.price is not None else product.price
    compare_at = variant.compare_at_price if variant.compare_at_price is not None else product.compare_at_price
    cost = variant.cost_price if variant.cost_price is not None else product.cost_price
    selling = Decimal(selling).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    compare_at = Decimal(compare_at).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if compare_at is not None else None
    cost = Decimal(cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if cost is not None else None
    if compare_at is not None and compare_at <= selling:
        raise ValidationError({"compare_at_price": "Effective compare-at price must exceed the selling price."})
    return {"selling_price": selling, "compare_at_price": compare_at, "cost_price": cost}


@transaction.atomic
def archive_variant(variant: StockVariant) -> StockVariant:
    variant = StockVariant.objects.select_for_update().get(pk=variant.pk)
    variant.is_active, variant.archived_at = False, timezone.now()
    variant.save(update_fields=["is_active", "archived_at", "updated_at"])
    return variant
