import io
import json
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from store.models import Store

from .models import CatalogSection, Product, StockVariant
from .serializers import MerchantProductSerializer, ProductSerializer


class RepairVariantSelectionsCommandTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Repair store", slug="repair-store")
        self.section = CatalogSection.objects.create(store=self.store, key="care", label="Care")

    def product(self, ref="TEST-KALYON-001"):
        product = Product.objects.create(
            store=self.store,
            section=self.section,
            name="KALYON adoucissant extra doux",
            ref=ref,
            price=Decimal("30.00"),
            uses_variants=True,
            variant_options=[{
                "key": "fragrance",
                "label": "Fragrance",
                "kind": "text",
                "type": "text",
                "values": ["lillian & lotus", "orchid flower"],
            }],
        )
        StockVariant.objects.create(
            store=self.store, product=product,
            attributes={"fragrance": "lillian & lotus"}, quantity=4,
        )
        StockVariant.objects.create(
            store=self.store, product=product,
            attributes={"fragrance": "orchid flower"}, quantity=2,
        )
        return product

    def run_command(self, *args):
        output = io.StringIO()
        call_command("repair_variant_selections", *args, stdout=output)
        return json.loads(output.getvalue())

    def test_dry_run_detects_repair_without_writing(self):
        product = self.product()
        report = self.run_command("--store-id", str(self.store.id), "--product-ref", product.ref)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["counts"], {"repairable": 1})
        self.assertEqual(product.options.count(), 0)
        self.assertFalse(product.variants.filter(selections__isnull=False).exists())

    def test_apply_reconstructs_text_selections_and_is_idempotent(self):
        product = self.product()
        first = self.run_command("--store-slug", self.store.slug, "--product-ref", product.ref, "--apply")
        self.assertEqual(first["counts"], {"repaired": 1})
        product.refresh_from_db()
        option = product.options.get()
        self.assertEqual((option.code, option.label, option.kind), ("fragrance", "Fragrance", "text"))
        self.assertEqual(list(option.values.values_list("code", flat=True)), ["lillian & lotus", "orchid flower"])
        self.assertTrue(all(variant.selections.count() == 1 for variant in product.variants.all()))
        identities = list(product.variants.values_list("id", "quantity"))

        second = self.run_command("--store-slug", self.store.slug, "--product-ref", product.ref, "--apply")
        self.assertEqual(second["counts"], {"ok": 1})
        self.assertEqual(list(product.variants.values_list("id", "quantity")), identities)

    def test_ambiguous_attributes_are_reported_and_never_guessed(self):
        product = self.product(ref="TEST-LILAS-001")
        variant = product.variants.first()
        variant.attributes = {}
        variant.save(update_fields=["attributes"])
        report = self.run_command("--store-id", str(self.store.id), "--product-ref", product.ref, "--apply")
        self.assertEqual(report["counts"], {"ambiguous": 1})
        self.assertEqual(report["products"][0]["reason"], "incomplete_variant_attributes")
        self.assertEqual(product.options.count(), 0)

    def test_dashboard_edit_updates_relational_contract_and_public_selection_ids(self):
        product = self.product()
        serializer = MerchantProductSerializer(
            product,
            data={
                "variant_options": [{
                    "key": "fragrance", "label": "Scent", "type": "text",
                    "values": ["lillian & lotus", "orchid flower"],
                }],
            },
            partial=True,
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save()
        self.assertEqual(product.options.get().label, "Scent")
        self.assertEqual(product.variant_options[0]["label"], "Scent")

        variant = product.variants.first()
        option = product.options.get()
        value = option.values.get(code=variant.attributes["fragrance"])
        from .variant_services import set_variant_selections
        set_variant_selections(variant, [(option, value)])
        payload = ProductSerializer(product).data
        selection = next(item for item in payload["variants"] if item["id"] == variant.id)["selections"][0]
        self.assertEqual(selection["option_id"], option.id)
        self.assertEqual(selection["value_id"], value.id)
        self.assertEqual(selection["option_label"], "Scent")
