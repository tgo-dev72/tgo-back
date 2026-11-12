import io
import json
import csv

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook, load_workbook
from rest_framework.test import APIClient

from store.models import Store, StoreMember

from .models import CatalogSection

User = get_user_model()


class MerchantSectionImportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="section-import@example.com", password="pass")
        self.store = Store.objects.create(name="Section import", slug="section-import")
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = f"/api/catalog/store/{self.store.pk}/sections/import/"

    def upload(self, name, content, content_type):
        return self.client.post(
            self.url,
            {"file": SimpleUploadedFile(name, content, content_type=content_type)},
            format="multipart",
        )

    def test_csv_creates_and_updates_sections_by_key(self):
        CatalogSection.objects.create(store=self.store, key="care", label="Old")
        response = self.upload(
            "sections.csv",
            "key,name_en,name_fr,name_ar,sort_order,is_visible\ncare,Care,Soins,عناية,2,true\nnew,New,Nouveau,,3,false\n".encode(),
            "text/csv",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"created": 1, "updated": 1, "total": 2})
        care = CatalogSection.objects.get(store=self.store, key="care")
        self.assertEqual(care.label_translations, {"en": "Care", "fr": "Soins", "ar": "عناية"})
        self.assertFalse(CatalogSection.objects.get(store=self.store, key="new").is_visible)

    def test_json_accepts_available_language_and_generates_key(self):
        response = self.upload(
            "sections.json",
            json.dumps({"sections": [{"name_en": "", "name_fr": "Beauté", "name_ar": ""}]}).encode(),
            "application/json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        section = CatalogSection.objects.get(store=self.store)
        self.assertEqual(section.key, "beaute")
        self.assertEqual(section.label, "Beauté")

    def test_product_export_json_gets_specific_guidance(self):
        response = self.upload(
            "wahajsahra-products.json",
            json.dumps({"schema": "tijarago.catalog", "version": 1, "products": []}).encode(),
            "application/json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["code"], "section_import_product_export")
        self.assertIn("product export", response.data["detail"])
        self.assertFalse(CatalogSection.objects.filter(store=self.store).exists())

    def test_xlsx_is_supported(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["key", "name_en", "name_fr", "name_ar", "sort_order", "is_visible"])
        sheet.append(["home", "Home", "Maison", "المنزل", 1, True])
        content = io.BytesIO()
        workbook.save(content)

        response = self.upload(
            "sections.xlsx",
            content.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(CatalogSection.objects.get(store=self.store).label_translations["ar"], "المنزل")

    def test_validation_errors_do_not_partially_import(self):
        response = self.upload(
            "sections.csv",
            b"key,name_en,sort_order\nvalid,Valid,1\nbad,Bad,-1\n",
            "text/csv",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["errors"][0]["row"], 3)
        self.assertEqual(response.data["detail"], 'Row 3, field "sort_order": Use a whole number of 0 or greater.')
        self.assertFalse(CatalogSection.objects.filter(store=self.store).exists())

    def test_templates_are_downloadable_in_all_supported_formats(self):
        base = f"/api/catalog/store/{self.store.pk}/sections/import-template/"
        csv_response = self.client.get(base, {"file_format": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["key", "name_en", "name_fr", "name_ar", "icon", "sort_order", "is_visible", "show_banner_overlay", "show_banner_title"])
        self.assertEqual(rows[1][0], "women")

        json_response = self.client.get(base, {"file_format": "json"})
        self.assertEqual(json_response.status_code, 200)
        self.assertEqual(json_response.json()["sections"][0]["name_ar"], "نساء")

        xlsx_response = self.client.get(base, {"file_format": "xlsx"})
        self.assertEqual(xlsx_response.status_code, 200)
        workbook = load_workbook(io.BytesIO(xlsx_response.content), read_only=True)
        self.assertEqual(workbook["Collections"]["A1"].value, "key")
        self.assertEqual(workbook["Instructions"]["A2"].value, "key")
