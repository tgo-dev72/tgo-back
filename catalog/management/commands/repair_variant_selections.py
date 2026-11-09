import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Product, ProductOption, StockVariant
from catalog.variant_services import configure_product_options, set_variant_selections


SUPPORTED_KINDS = set(ProductOption.Kind.values)


def _definitions(product):
    definitions = []
    for position, item in enumerate(product.variant_options or []):
        if not isinstance(item, dict):
            raise ValidationError("Option definitions must be objects.")
        values = item.get("values") or []
        raw_values = [str(value.get("value", "") if isinstance(value, dict) else value).strip() for value in values]
        kind = item.get("kind")
        if kind not in SUPPORTED_KINDS:
            if item.get("type") == "color":
                kind = ProductOption.Kind.COLOR
            elif item.get("key") == "size" and raw_values and all(value.isdigit() for value in raw_values):
                kind = ProductOption.Kind.SIZE_NUMERIC
            elif item.get("key") == "size" and raw_values:
                kind = ProductOption.Kind.SIZE_ALPHA
            else:
                kind = ProductOption.Kind.TEXT
        definitions.append({
            "code": item.get("key"),
            "name": item.get("label"),
            "kind": kind,
            "presentation": item.get("presentation"),
            "values": values,
            "position": position,
        })
    return definitions


def audit_product(product):
    variants = list(product.variants.order_by("id"))
    definitions = _definitions(product)
    if not product.uses_variants:
        return {"status": "ok", "reason": "simple_product", "variants": len(variants)}
    if not definitions:
        return {"status": "ambiguous", "reason": "missing_option_definitions", "variants": len(variants)}
    if not variants:
        return {"status": "ambiguous", "reason": "missing_variants", "variants": 0}

    codes = [str(item.get("code") or "").strip() for item in definitions]
    if any(not code for code in codes) or len(codes) != len(set(codes)):
        return {"status": "ambiguous", "reason": "invalid_option_identities", "variants": len(variants)}

    allowed = {}
    for definition in definitions:
        values = definition["values"]
        normalized = {}
        for value in values:
            code = str(value.get("value", "") if isinstance(value, dict) else value).strip()
            folded = code.casefold()
            if not code or folded in normalized:
                return {"status": "ambiguous", "reason": "invalid_or_duplicate_option_values", "variants": len(variants)}
            normalized[folded] = code
        if not normalized:
            return {"status": "ambiguous", "reason": "missing_option_values", "variants": len(variants)}
        allowed[definition["code"]] = normalized

    combinations = set()
    for variant in variants:
        attributes = variant.attributes if isinstance(variant.attributes, dict) else {}
        if set(attributes) != set(codes):
            return {"status": "ambiguous", "reason": "incomplete_variant_attributes", "variant_id": variant.id}
        signature = []
        for code in codes:
            value = str(attributes[code]).strip()
            canonical = allowed[code].get(value.casefold())
            if canonical is None:
                return {"status": "ambiguous", "reason": "unknown_option_value", "variant_id": variant.id, "option": code}
            signature.append((code, canonical.casefold()))
        signature = tuple(signature)
        if signature in combinations:
            return {"status": "ambiguous", "reason": "duplicate_combination", "variant_id": variant.id}
        combinations.add(signature)

    active_options = list(product.options.filter(is_active=True).prefetch_related("values"))
    complete = len(active_options) == len(definitions)
    option_by_code = {option.code: option for option in active_options}
    if complete:
        for definition in definitions:
            option = option_by_code.get(definition["code"])
            expected = set(allowed[definition["code"]])
            actual = {value.code.casefold() for value in option.values.filter(is_active=True)} if option else set()
            if option is None or option.kind != definition["kind"] or actual != expected:
                complete = False
                break
    if complete:
        for variant in variants:
            selected = {selection.option.code: selection.value.code.casefold() for selection in variant.selections.select_related("option", "value")}
            expected = {code: str(variant.attributes[code]).strip().casefold() for code in codes}
            if selected != expected or not variant.combination_key:
                complete = False
                break
    return {"status": "ok" if complete else "repairable", "variants": len(variants)}


class Command(BaseCommand):
    help = "Audit legacy variant attributes and reconstruct relational selections only when unambiguous."

    def add_arguments(self, parser):
        parser.add_argument("--store-id", type=int)
        parser.add_argument("--store-slug")
        parser.add_argument("--product-ref")
        parser.add_argument("--apply", action="store_true", help="Apply repairable changes; default is dry-run.")

    def handle(self, *args, **options):
        if not options["store_id"] and not options["store_slug"]:
            raise CommandError("Scope the audit with --store-id or --store-slug.")
        products = Product.objects.select_related("store").prefetch_related(
            "options__values", "variants__selections__option", "variants__selections__value",
        ).order_by("id")
        if options["store_id"]:
            products = products.filter(store_id=options["store_id"])
        if options["store_slug"]:
            products = products.filter(store__slug=options["store_slug"])
        if options["product_ref"]:
            products = products.filter(ref=options["product_ref"])
        if not products.exists():
            raise CommandError("No products matched this scope.")

        report = {"mode": "apply" if options["apply"] else "dry-run", "products": []}
        for product in products:
            result = audit_product(product)
            row = {"product_id": product.id, "store": product.store.slug, "ref": product.ref, **result}
            if result["status"] == "repairable" and options["apply"]:
                try:
                    with transaction.atomic():
                        locked = Product.objects.select_for_update().get(pk=product.pk)
                        definitions = _definitions(locked)
                        configured = configure_product_options(locked, definitions)
                        by_code = {option.code: option for option in configured}
                        for variant in StockVariant.objects.select_for_update().filter(product=locked):
                            selections = []
                            for code, raw in variant.attributes.items():
                                option = by_code[code]
                                value = option.values.get(code__iexact=str(raw).strip(), is_active=True)
                                selections.append((option, value))
                            set_variant_selections(variant, selections)
                    row["status"] = "repaired"
                except (ValidationError, KeyError, ProductOption.DoesNotExist) as exc:
                    row.update(status="ambiguous", reason="repair_validation_failed", detail=str(exc))
            report["products"].append(row)
        counts = {}
        for row in report["products"]:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        report["counts"] = counts
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
