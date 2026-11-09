import json
from collections import defaultdict

from django.core.management.base import BaseCommand

from catalog.models import ProductImage, StockVariant


class Command(BaseCommand):
    help = "Report catalog conflicts that must be resolved before relational option backfill."

    def add_arguments(self, parser):
        parser.add_argument("--store-id", type=int)

    def handle(self, *args, **options):
        variants = StockVariant.objects.order_by("store_id", "product_id", "id")
        images = ProductImage.objects.filter(is_primary=True).order_by("product_id", "id")
        if options["store_id"]:
            variants = variants.filter(store_id=options["store_id"])
            images = images.filter(product__store_id=options["store_id"])
        combinations, duplicate_combinations, ambiguous_attributes = defaultdict(list), [], []
        variant_rows = list(variants.values("id", "store_id", "product_id", "product__uses_variants", "attributes", "reserved"))
        for variant in variant_rows:
            if not isinstance(variant["attributes"], dict):
                ambiguous_attributes.append({"variant_id": variant["id"], "reason": "attributes_not_object"})
                continue
            normalized = tuple(sorted((str(key).strip().casefold(), str(value).strip().casefold()) for key, value in variant["attributes"].items()))
            combinations[(variant["product_id"], normalized)].append(variant["id"])
            if variant["product__uses_variants"] and not normalized:
                ambiguous_attributes.append({"variant_id": variant["id"], "reason": "variant_product_empty_selection"})
        for (product_id, normalized), ids in combinations.items():
            if len(ids) > 1:
                duplicate_combinations.append({"product_id": product_id, "normalized": normalized, "variant_ids": ids})
        primaries = defaultdict(list)
        for image in images.values("id", "product_id").iterator():
            primaries[image["product_id"]].append(image["id"])
        multiple_primary_images = [
            {"product_id": product_id, "image_ids": ids}
            for product_id, ids in primaries.items() if len(ids) > 1
        ]
        reservation_dependencies = [
            {"variant_id": item["id"], "product_id": item["product_id"], "reserved": item["reserved"]}
            for item in variant_rows if item["reserved"] > 0
        ]
        report = {
            "safe": not (duplicate_combinations or ambiguous_attributes or multiple_primary_images),
            "duplicate_combinations": duplicate_combinations,
            "ambiguous_attributes": ambiguous_attributes,
            "multiple_primary_images": multiple_primary_images,
            "reservation_dependencies": reservation_dependencies,
            "action": "Resolve every reported conflict explicitly; this command never merges or deletes records.",
        }
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=list))
