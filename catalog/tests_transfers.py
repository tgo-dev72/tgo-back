from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from io import BytesIO, StringIO
import csv
import json
from unittest.mock import patch
from openpyxl import Workbook, load_workbook
from rest_framework.test import APIClient

from store.models import Store, StoreMember
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan
from subscriptions.services import ensure_free_subscription

from .models import CatalogImportOperation, CatalogSection, Product, RetailType, StockVariant
from .serializers import ProductImageReadSerializer
from .transfers import PARAMETER_FIELDS, TWO_SHEET_PRODUCT_FIELDS, TWO_SHEET_VARIANT_FIELDS


class CatalogTransferTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(email="transfer@example.test", password="pass")
        self.other = get_user_model().objects.create_user(email="other-transfer@example.test", password="pass")
        retail = RetailType.objects.get(key="general")
        self.store = Store.objects.create(name="Export", slug="export", retail_type=retail)
        self.other_store = Store.objects.create(name="Other", slug="other-export", retail_type=retail)
        StoreMember.objects.create(user=self.user, store=self.store, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(user=self.other, store=self.other_store, role=StoreMember.Roles.OWNER)
        self.section = CatalogSection.objects.create(store=self.store, key="general", label="General")
        CatalogSection.objects.create(store=self.other_store, key="general", label="General")
        transfer_definition = EntitlementDefinition.objects.get(key="catalog.import")
        PlanEntitlement.objects.update_or_create(
            plan=SubscriptionPlan.objects.get(slug="free"),
            definition=transfer_definition,
            defaults={"value": True},
        )
        ensure_free_subscription(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_basic_export_remains_available_when_import_is_disabled(self):
        subscription = self.user.platform_subscriptions.get(status="active")
        subscription.entitlements_snapshot["catalog.import"] = False
        subscription.entitlements_snapshot["catalog.import_export"] = True
        subscription.save(update_fields=["entitlements_snapshot"])
        exported = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv")
        imported = self.upload("ref,name,section,price\nNEW,New,general,2\n", "not-included")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(imported.status_code, 403)
        self.assertEqual(imported.data["rule"], "catalog.import")
        capabilities = self.client.get(
            f"/api/catalog/store/{self.store.pk}/transfer-capabilities/",
        )
        self.assertTrue(capabilities.data["export"]["allowed"])
        self.assertFalse(capabilities.data["import"]["allowed"])
        self.assertEqual(capabilities.data["import"]["entitlement"], "catalog.import")
        self.assertEqual(capabilities.data["export"]["label"], "تنزيل قائمة المنتجات")
        self.assertEqual(capabilities.data["import"]["label"], "إضافة منتجات من ملف")
        self.assertNotIn("permission", capabilities.data["export"]["description"].lower())
        self.assertNotIn("paid plan", capabilities.data["export"]["description"].lower())

    def test_import_guide_and_store_specific_templates_are_available(self):
        guide = self.client.get(f"/api/catalog/store/{self.store.pk}/import-guide/")
        self.assertEqual(guide.status_code, 200)
        self.assertEqual(guide.data["title"], "إضافة منتجات من ملف")
        self.assertEqual(guide.data["sections"], [{"key": "general", "label": "General"}])
        self.assertIsNone(guide.data["limits"]["maximum_products"])
        self.assertIn("xlsx", guide.data["templates"])

        french = self.client.get(
            f"/api/catalog/store/{self.store.pk}/transfer-capabilities/?lang=fr",
        )
        self.assertEqual(french.status_code, 200)
        self.assertEqual(french.data["guide"]["title"], "Ajouter des produits depuis un fichier")
        self.assertTrue(all("نزّل" not in step for step in french.data["guide"]["steps"]))
        self.assertEqual(french.data["import"]["label"], "Ajouter depuis un fichier")

        csv_template = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=csv",
        )
        self.assertEqual(csv_template.status_code, 200)
        csv_rows = list(csv.reader(StringIO(csv_template.content.decode("utf-8-sig"))))
        self.assertEqual(csv_rows[0][:4], ["ref", "name", "section", "price"])

        json_sample = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=json&lang=en",
        )
        self.assertEqual(json_sample.status_code, 200)
        sample = json.loads(json_sample.content)
        self.assertEqual(sample["schema"], "tijarago.catalog")
        self.assertEqual(sample["products"][0]["section"], "general")
        self.assertTrue(sample["products"][0]["variants"])
        validation = self.upload_json(sample, "json-sample-template")
        self.assertTrue(validation.data["valid"], validation.data)
        self.assertEqual(csv_rows[1][2], "general")

        excel_template = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=xlsx",
        )
        workbook = load_workbook(BytesIO(excel_template.content), read_only=True)
        self.assertEqual(workbook.sheetnames, ["Products", "Variants", "Defaults", "Guide"])
        self.assertEqual(
            tuple(cell.value for cell in workbook["Products"][1][:len(TWO_SHEET_PRODUCT_FIELDS)]),
            TWO_SHEET_PRODUCT_FIELDS,
        )
        self.assertEqual(tuple(cell.value for cell in workbook["Variants"][1][:19]), TWO_SHEET_VARIANT_FIELDS)
        editable = load_workbook(BytesIO(excel_template.content))
        validations = editable["Variants"].data_validations.dataValidation
        self.assertFalse(any("INDIRECT" in str(item.formula1) for item in validations))
        self.assertTrue(any("$Y2:$BH2" in str(item.formula1) for item in validations))

        french = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=xlsx&lang=fr",
        )
        french_workbook = load_workbook(BytesIO(french.content), read_only=True)
        self.assertEqual(french_workbook.sheetnames, ["Products", "Variants", "Defaults", "Guide"])
        english = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=xlsx&lang=en",
        )
        english_workbook = load_workbook(BytesIO(english.content), read_only=True)
        self.assertEqual(english_workbook.sheetnames, ["Products", "Variants", "Defaults", "Guide"])
        template_validation = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile(
                "products-template.xlsx", excel_template.content,
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )},
            HTTP_IDEMPOTENCY_KEY="generated-template",
        )
        self.assertTrue(template_validation.data["valid"], template_validation.data)
        self.assertEqual(template_validation.data["schema"], "native-two-sheet-v3")
        self.assertEqual(template_validation.data["result"]["preview"]["products"], 0)
        self.assertEqual(template_validation.data["result"]["preview"]["variants"], 0)
        self.assertEqual(template_validation.data["result"]["preview"]["stock"], 0)
        self.assertFalse(Product.objects.filter(store=self.store, ref="TSHIRT-001").exists())

    def test_two_sheet_reports_structured_cross_sheet_errors(self):
        template = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=xlsx&lang=en",
        )
        workbook = load_workbook(BytesIO(template.content))
        products, variants = workbook["Products"], workbook["Variants"]
        product_row = ("BROKEN", "Broken", "general", 10, "", True, 0, "", True, True, "broken", 12, 5, "", "", "", "", False, None, "")
        for column, value in enumerate(product_row, 1): products.cell(2, column, value)
        variant_rows = (
            ("BROKEN", "DUP-SKU", "size_alpha", "Size", "M", "color", "Color", "Black", "", "", "", None, None, None, 1, "", None, "", True),
            ("BROKEN", "DUP-SKU", "size_alpha", "Size", "L", "color", "Color", "Black", "", "", "", None, None, None, 1, "", None, "", True),
            ("ORPHAN", "ORPHAN-SKU", "size_alpha", "Size", "M", "", "", "", "", "", "", None, None, None, 1, "", None, "", True),
            ("BROKEN", "INCOMPLETE", "size_alpha", "Size", "XL", "color", "Color", "", "", "", "", None, None, None, 1, "", None, "", True),
        )
        for row_number, row in enumerate(variant_rows, 2):
            for column, value in enumerate(row, 1): variants.cell(row_number, column, value)
        output = BytesIO(); workbook.save(output)
        response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile(
                "invalid-two-sheet.xlsx", output.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )},
            HTTP_IDEMPOTENCY_KEY="invalid-two-sheet",
        )
        self.assertFalse(response.data["valid"])
        codes = {error.get("code") for error in response.data["errors"]}
        self.assertTrue({"variant_stock_conflict", "sku_conflict", "orphan_variant", "incomplete_option"} <= codes, codes)
        self.assertTrue(all(error.get("sheet") and error.get("row") and error.get("column") for error in response.data["errors"]))

    def test_native_options_come_from_actual_rows_and_do_not_generate_combinations(self):
        template = self.client.get(
            f"/api/catalog/store/{self.store.pk}/import-template/?file_format=xlsx&lang=ar",
        )
        workbook = load_workbook(BytesIO(template.content))
        products, variants = workbook["Products"], workbook["Variants"]
        product_row = ("TSHIRT-001", "Shirt", "general", 149, "", True, None, "", True, True, "shirt", None, None, "", "", "", "", False, None, "")
        for column, value in enumerate(product_row, 1): products.cell(2, column, value)
        variant_rows = (
            ("TSHIRT-001", "TSHIRT-M-GRN", "size_alpha", "Size", "M", "color", "Color", "Green", "", "", "", None, None, None, 10, "", None, "", True),
            ("TSHIRT-001", "TSHIRT-XL-BLK", "size_alpha", "Size", "XL", "color", "Color", "Black", "", "", "", None, None, None, 8, "", None, "", True),
        )
        for row_number, row in enumerate(variant_rows, 2):
            for column, value in enumerate(row, 1): variants.cell(row_number, column, value)
        output = BytesIO(); workbook.save(output)
        response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile(
                "invalid-parameters.xlsx", output.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )},
            HTTP_IDEMPOTENCY_KEY="invalid-parameters",
        )
        self.assertTrue(response.data["valid"], response.data)
        group = next(item for item in response.data["result"]["preview"]["groups"] if item["ref"] == "TSHIRT-001")
        self.assertIn({"size": "M", "color": "Green"}, group["combinations"])
        self.assertNotIn({"size": "XL", "color": "White"}, group["combinations"])

    def upload(self, content, key="import-1", **options):
        return self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile("products.csv", content.encode("utf-8"), content_type="text/csv"), **options},
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def upload_json(self, payload, key="json-import", **policy):
        return self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile(
                "products.json", json.dumps(payload).encode("utf-8"), content_type="application/json",
            ), "default_section": "general", **policy},
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_export_is_tenant_scoped_unicode_and_formula_safe(self):
        Product.objects.create(store=self.store, section=self.section, ref="=CMD()", name="منتج Français", price="12.50")
        other_section = self.other_store.catalog_sections.get(key="general")
        Product.objects.create(store=self.other_store, section=other_section, ref="OTHER", name="Secret", price="1")
        response = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv")
        body = response.content.decode("utf-8-sig")
        self.assertEqual(response.status_code, 200)
        self.assertIn("منتج Français", body)
        self.assertIn("'=CMD()", body)
        self.assertNotIn("Secret", body)
        exported_rows = list(csv.DictReader(StringIO(body)))
        self.assertNotIn("tax_rate", exported_rows[0])
        self.assertEqual(self.client.get(f"/api/catalog/store/{self.other_store.pk}/export.csv").status_code, 404)
        self.assertEqual(self.upload(
            "ref,name,section,price\nCROSS,Cross,general,2\n", "cross-merchant",
        ).status_code, 201)
        cross_validate = self.client.post(
            f"/api/catalog/store/{self.other_store.pk}/imports/validate/",
            {"file": SimpleUploadedFile("products.csv", b"ref,name,section,price\nX,X,general,1\n")},
            HTTP_IDEMPOTENCY_KEY="cross-validate",
        )
        self.assertEqual(cross_validate.status_code, 404)
        operation = CatalogImportOperation.objects.get(
            store=self.store, idempotency_key="cross-merchant",
        )
        cross_commit = self.client.post(
            f"/api/catalog/store/{self.other_store.pk}/imports/{operation.pk}/commit/",
        )
        self.assertEqual(cross_commit.status_code, 404)

        excel = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv?file_format=xlsx")
        workbook = load_workbook(BytesIO(excel.content), read_only=True, data_only=False)
        excel_rows = list(workbook.active.iter_rows(values_only=True))
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(excel_rows[0][:4], ("ref", "name", "section", "price"))
        self.assertEqual(excel_rows[1][0], "'=CMD()")
        self.assertEqual(excel_rows[1][1], "منتج Français")

        exported_json = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv?file_format=json")
        self.assertNotIn("tax_rate", exported_json.json()["products"][0])

    def test_excel_validation_and_commit_support_unicode(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ref", "name", "section", "price", "description", "is_active", "quantity"])
        sheet.append(["XLSX-1", "منتج Français", "general", 9.5, "وصف", True, 7])
        output = BytesIO()
        workbook.save(output)
        response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile(
                "products.xlsx", output.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )},
            HTTP_IDEMPOTENCY_KEY="xlsx-import",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["valid"], response.data)
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{response.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(store=self.store, ref="XLSX-1")
        self.assertEqual(product.name, "منتج Français")
        self.assertEqual(product.variants.get().quantity, 7)

    def test_csv_repeated_rows_create_and_export_product_variants(self):
        csv_data = (
            "ref,name,section,price,uses_variants,option1_key,option1_name,option1_type,option1_value,option2_key,option2_name,option2_type,option2_value,option2_swatch,variant_sku,variant_price,variant_quantity\n"
            "SHIRT-1,قميص,general,100,true,size,المقاس,select,M,color,اللون,color,أحمر,#c62828,SHIRT-M-RED,100,5\n"
            "SHIRT-1,قميص,general,100,true,size,المقاس,select,L,color,اللون,color,أحمر,#c62828,SHIRT-L-RED,110,3\n"
        )
        validation = self.upload(csv_data, "csv-variants")
        self.assertEqual(validation.status_code, 201, validation.data)
        self.assertTrue(validation.data["valid"], validation.data)
        self.assertEqual(validation.data["products"], 1)
        self.assertEqual(validation.data["schema"], "tabular-variants-v2")
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual(committed.data["result"]["variants"], 2)
        product = Product.objects.get(store=self.store, ref="SHIRT-1")
        self.assertTrue(product.uses_variants)
        self.assertEqual(product.variant_options[0]["values"], ["M", "L"])
        self.assertEqual(product.variants.get(sku="SHIRT-L-RED").attributes, {"size": "L", "color": "أحمر"})
        self.assertEqual([item["label"] for item in product.variant_options], ["المقاس", "اللون"])
        self.assertEqual(product.variant_options[1]["type"], "color")
        self.assertEqual(product.variant_options[1]["values"][0]["swatch"], "#c62828")

        exported = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv")
        rows = list(csv.DictReader(StringIO(exported.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["variant_sku"] for row in rows}, {"SHIRT-M-RED", "SHIRT-L-RED"})
        self.assertEqual({row["option1_value"] for row in rows}, {"M", "L"})
        self.assertEqual({row["option2_type"] for row in rows}, {"color"})
        self.assertEqual({row["option2_swatch"] for row in rows}, {"#c62828"})

    def test_xlsx_export_preserves_variant_rows_and_columns(self):
        product = Product.objects.create(
            store=self.store, section=self.section, ref="SHOE-1", name="حذاء", price="200",
            uses_variants=True,
            variant_options=[{"key": "size", "label": "المقاس", "type": "select", "values": ["40", "41"]}],
        )
        StockVariant.objects.create(store=self.store, product=product, attributes={"size": "40"}, sku="SHOE-40", quantity=2)
        StockVariant.objects.create(store=self.store, product=product, attributes={"size": "41"}, sku="SHOE-41", quantity=4)
        response = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv?file_format=xlsx")
        workbook = load_workbook(BytesIO(response.content), read_only=True, data_only=False)
        rows = list(workbook.active.iter_rows(values_only=True))
        header = list(rows[0])
        self.assertEqual(len(rows), 3)
        self.assertIn("variant_sku", header)
        self.assertEqual({row[header.index("variant_sku")] for row in rows[1:]}, {"SHOE-40", "SHOE-41"})

    def test_tabular_types_are_rejected_server_side(self):
        invalid = self.upload(
            "ref,name,section,price,is_active,uses_variants,option1_name,option1_value,variant_quantity\n"
            "BAD-TYPES,Invalid,general,not-a-price,maybe,true,Size,M,1.5\n",
            "invalid-tabular-types",
        )
        self.assertEqual(invalid.status_code, 201, invalid.data)
        self.assertFalse(invalid.data["valid"])
        fields = invalid.data["errors"][0]["fields"]
        self.assertIn("price", fields)
        self.assertIn("is_active", fields)
        self.assertIn("variants.1.quantity", fields)

    def test_common_multilingual_color_names_resolve_without_manual_hex(self):
        data = (
            "ref,name,section,price,uses_variants,option1_key,option1_name,option1_type,option1_value,variant_sku,variant_quantity\n"
            "COLOR-AR,ألوان,general,10,true,color,اللون,color,أحمر,COLOR-RED,2\n"
            "COLOR-AR,ألوان,general,10,true,color,اللون,color,Blue,COLOR-BLUE,3\n"
            "COLOR-AR,ألوان,general,10,true,color,اللون,color,Vert,COLOR-GREEN,4\n"
        )
        validation = self.upload(data, "known-colors")
        self.assertTrue(validation.data["valid"], validation.data)
        operation = CatalogImportOperation.objects.get(pk=validation.data["id"])
        values = operation.rows[0]["variant_options"][0]["values"]
        self.assertEqual([item["swatch"] for item in values], ["#c62828", "#1565c0", "#2e7d32"])

    def test_exported_simple_product_reimports_without_enabling_variants(self):
        Product.objects.create(store=self.store, section=self.section, ref="SIMPLE-1", name="Simple", price="20")
        exported = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv")
        validation = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/validate/",
            {"file": SimpleUploadedFile("products.csv", exported.content, content_type="text/csv")},
            HTTP_IDEMPOTENCY_KEY="simple-round-trip",
        )
        self.assertTrue(validation.data["valid"], validation.data)
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(ref="SIMPLE-1")
        self.assertFalse(product.uses_variants)
        self.assertEqual(product.variants.count(), 1)

    def test_validate_then_atomic_commit_updates_creates_and_retry_is_idempotent(self):
        Product.objects.create(store=self.store, section=self.section, ref="OLD", name="Old", price="1")
        csv_data = "ref,name,section,price,description,is_active,quantity\nOLD,Updated,general,2.00,é,true,3\nNEW,جديد,general,4.25,,true,5\n"
        validation = self.upload(csv_data)
        self.assertEqual(validation.status_code, 201, validation.data)
        self.assertTrue(validation.data["valid"])
        url = f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/"
        first = self.client.post(url)
        second = self.client.post(url)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(
            {key: first.data["result"][key] for key in ("created", "updated", "rejected", "failed")},
            {"created": 1, "updated": 1, "rejected": 0, "failed": 0},
        )
        self.assertEqual(second.data["result"], first.data["result"])
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2)
        self.assertEqual(Product.objects.get(store=self.store, ref="NEW").variants.get().quantity, 5)

    def test_large_import_commit_is_resumable_across_atomic_batches(self):
        rows = ["ref,name,section,price"] + [f"BATCH-{index},Product {index},general,1" for index in range(31)]
        validation = self.upload("\n".join(rows) + "\n", "resumable-batches")
        self.assertTrue(validation.data["valid"], validation.data)
        url = f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/"
        first = self.client.post(url)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["status"], "validated")
        self.assertEqual(first.data["result"]["processed"], 25)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 25)
        second = self.client.post(url)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["status"], "committed")
        self.assertEqual(second.data["result"]["processed"], 31)
        self.assertEqual(second.data["result"]["created"], 31)
        self.assertEqual(Product.objects.filter(store=self.store).count(), 31)

    def test_validation_rejects_duplicate_and_commit_enforces_product_quota(self):
        duplicate = self.upload("ref,name,section,price\nA,One,general,1\nA,Two,general,2\n", "dupe")
        self.assertFalse(duplicate.data["valid"])
        blocked = self.client.post(f"/api/catalog/store/{self.store.pk}/imports/{duplicate.data['id']}/commit/")
        self.assertEqual(blocked.status_code, 409)

        definition = EntitlementDefinition.objects.get(key="catalog.max_products")
        PlanEntitlement.objects.update_or_create(
            plan=SubscriptionPlan.objects.get(slug="free"), definition=definition, defaults={"value": 1},
        )
        Product.objects.create(store=self.store, section=self.section, ref="EXISTING", name="Existing", price="1")
        valid = self.upload("ref,name,section,price\nNEW,New,general,2\n", "quota")
        denied = self.client.post(f"/api/catalog/store/{self.store.pk}/imports/{valid.data['id']}/commit/")
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Product.objects.filter(store=self.store, ref="NEW").exists())

    def test_native_json_round_trip_preserves_variants_and_product_metadata(self):
        payload = {
            "schema": "tijarago.catalog", "version": 1,
            "products": [{
                "ref": "JSON-1", "name": "Chemise", "slug": "chemise", "section": "general",
                "price": "100.00", "compare_at_price": "120.00", "tags": ["summer", "sale"],
                "seo_title": "Chemise SEO", "is_featured": True, "uses_variants": True,
                "variant_options": [{"key": "size", "label": "Size", "type": "select", "values": ["S", "M"]}],
                "variants": [
                    {"attributes": {"size": "S"}, "sku": "SHIRT-S", "price": "100", "quantity": 3},
                    {"attributes": {"size": "M"}, "sku": "SHIRT-M", "price": "105", "quantity": 4},
                ],
            }],
        }
        validation = self.upload_json(payload)
        self.assertEqual(validation.status_code, 201, validation.data)
        self.assertTrue(validation.data["valid"], validation.data)
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(store=self.store, ref="JSON-1")
        self.assertEqual(product.tags, ["summer", "sale"])
        self.assertEqual(product.variants.count(), 2)
        self.assertEqual(product.variants.get(sku="SHIRT-M").attributes, {"size": "M"})
        exported = self.client.get(f"/api/catalog/store/{self.store.pk}/export.csv?file_format=json")
        document = json.loads(exported.content)
        self.assertEqual(document["schema"], "tijarago.catalog")
        self.assertEqual(document["products"][0]["variants"][0]["sku"], "SHIRT-S")

    @patch("catalog.remote_media.download_image", side_effect=AssertionError("commit must not download remote media"))
    def test_external_products_json_is_detected_and_normalized(self, _download_image):
        payload = {"products": [{
            "id": 123, "title": "Dove Shampoo", "handle": "dove-shampoo",
            "body_html": "<p>Care</p>", "vendor": "Dove", "product_type": "Unknown External Type",
            "tags": ["hair"], "published_at": "2026-01-01T00:00:00Z",
            "options": [{"name": "Color", "values": ["Blue", "Pink"]}],
            "variants": [
                {"id": 10, "option1": "Blue", "price": "12.50", "sku": "DOVE-B", "inventory_quantity": 6, "featured_image": {"id": 501, "src": "https://cdn.example.com/catalog/product.webp"}},
                {"id": 11, "option1": "Pink", "price": "13.00", "sku": "DOVE-P"},
            ],
            "images": [{"id": 501, "src": "https://cdn.example.com/catalog/product.webp", "position": 1, "alt": "Dove Shampoo", "variant_ids": [10]}],
        }]}
        validation = self.upload_json(payload, "external-json")
        self.assertTrue(validation.data["valid"], validation.data)
        self.assertEqual(validation.data["schema"], "external-products-json-v1")
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(ref="export-123")
        self.assertEqual(product.vendors.get().name, "Dove")
        self.assertEqual(product.variant_options[0]["type"], "color")
        self.assertEqual(product.variants.get(sku="DOVE-B").attributes, {"color": "Blue"})
        self.assertTrue(product.variants.get(sku="DOVE-B").is_active)
        self.assertEqual(product.variants.get(sku="DOVE-B").quantity, 6)
        self.assertTrue(product.variants.get(sku="DOVE-P").is_active)
        self.assertEqual(product.variants.get(sku="DOVE-P").quantity, 0)
        image = product.images.get()
        self.assertFalse(bool(image.image))
        self.assertEqual(image.external_url, "https://cdn.example.com/catalog/product.webp")
        self.assertEqual(ProductImageReadSerializer(image).data["image"], image.external_url)
        self.assertEqual(product.variants.get(sku="DOVE-B").image_id, image.id)

    def test_external_policy_can_publish_set_fallback_stock_and_skip_unavailable(self):
        payload = {"products": [{
            "id": 456, "title": "Flexible import", "handle": "flexible-import",
            "variants": [
                {"id": 20, "title": "Ready", "price": "8.00", "sku": "READY", "available": True},
                {"id": 21, "title": "Unavailable", "price": "8.00", "sku": "NOPE", "available": False},
            ],
        }]}
        validation = self.upload_json(
            payload, "external-policy", publish_products="true",
            include_unavailable="false", default_available_stock="12",
            default_unavailable_stock="4",
        )
        self.assertTrue(validation.data["valid"], validation.data)
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{validation.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(ref="export-456")
        self.assertTrue(product.is_active)
        self.assertEqual(product.variants.get().sku, "READY")
        self.assertEqual(product.variants.get().quantity, 12)

    def test_unknown_section_can_be_approved_and_created_atomically(self):
        content = "ref,name,section,price,quantity\nNEW-1,New product,Seasonal Offers,4.00,3\n"
        initial = self.upload(content, "unknown-section")
        self.assertFalse(initial.data["valid"])
        self.assertEqual(initial.data["result"]["unknown_sections"], ["Seasonal Offers"])
        approved = self.upload(
            content, "approved-section",
            section_resolutions=json.dumps({"Seasonal Offers": "__create__"}),
        )
        self.assertTrue(approved.data["valid"], approved.data)
        self.assertFalse(CatalogSection.objects.filter(store=self.store, key="seasonal-offers").exists())
        committed = self.client.post(
            f"/api/catalog/store/{self.store.pk}/imports/{approved.data['id']}/commit/",
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        product = Product.objects.get(store=self.store, ref="NEW-1")
        self.assertEqual(product.section.key, "seasonal-offers")
