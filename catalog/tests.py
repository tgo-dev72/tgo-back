import importlib
from copy import deepcopy
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.files.storage import FileSystemStorage
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from store.models import Store, StoreMember
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan
from subscriptions.services import ensure_free_subscription

from .models import (
    AttributeDef, CatalogSection, Product, ProductReview, ProductVendor,
    RetailType, StockVariant, product_image_storage, validate_retail_type_config,
)
from .retail_presets import RETAIL_TYPE_PRESETS, RETAIL_TYPE_SECTIONS
from .seeds import DEFAULT_SECTIONS, ensure_default_sections, ensure_retail_types
from .serializers import (
    MerchantAttributeDefSerializer, MerchantProductSerializer, MerchantSectionSerializer,
    MerchantVariantSerializer, ProductImageWriteSerializer, ProductSerializer,
)
from common.media_uploads import image_folder, signed_upload_payload


User = get_user_model()


class PublicCatalogPaginationTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Paged store", slug="paged-store")
        self.section = CatalogSection.objects.create(
            store=self.store, key="care", label="Care",
        )
        for index in range(5):
            Product.objects.create(
                store=self.store, section=self.section, name=f"Product {index}",
                ref=f"PAGE-{index}", price="10.00", is_featured=index == 0,
            )
        other = Store.objects.create(name="Other store", slug="other-paged")
        other_section = CatalogSection.objects.create(
            store=other, key="care", label="Care",
        )
        Product.objects.create(
            store=other, section=other_section, name="Other product",
            ref="OTHER-1", price="10.00",
        )

    def test_opt_in_pages_are_bounded_and_tenant_scoped(self):
        first = self.client.get(
            "/api/catalog/products/?limit=2&offset=0",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        second = self.client.get(
            "/api/catalog/products/?limit=2&offset=2",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json()["count"], 5)
        self.assertEqual(len(first.json()["results"]), 2)
        self.assertEqual(len(second.json()["results"]), 2)
        self.assertFalse(
            {item["id"] for item in first.json()["results"]}
            & {item["id"] for item in second.json()["results"]}
        )
        self.assertNotIn("Other product", [item["name"] for item in first.json()["results"]])
        featured = self.client.get(
            "/api/catalog/products/?limit=2&source=featured",
            HTTP_X_STORE_SLUG=self.store.slug,
        ).json()
        self.assertEqual(featured["count"], 1)
        self.assertEqual(featured["results"][0]["name"], "Product 0")
        legacy = self.client.get(
            "/api/catalog/products/", HTTP_X_STORE_SLUG=self.store.slug,
        ).json()
        self.assertIsInstance(legacy, list)
        self.assertEqual(len(legacy), 5)

    def test_discounted_page_orders_by_effective_customer_discount(self):
        Product.objects.create(
            store=self.store, section=self.section, name="Compare-at deal",
            ref="COMPARE-40", price="60.00", compare_at_price="100.00", promo=5,
        )
        Product.objects.create(
            store=self.store, section=self.section, name="Promo deal",
            ref="PROMO-25", price="10.00", promo=25,
        )
        response = self.client.get(
            "/api/catalog/products/?limit=1&source=discounted",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(response.json()["results"][0]["name"], "Compare-at deal")

    def test_invalid_pagination_is_rejected(self):
        for query in ("limit=0", "limit=49", "limit=x", "limit=2&offset=-1"):
            with self.subTest(query=query):
                response = self.client.get(
                    f"/api/catalog/products/?{query}",
                    HTTP_X_STORE_SLUG=self.store.slug,
                )
                self.assertEqual(response.status_code, 400, response.content)

    def test_section_names_follow_language_with_english_then_available_fallback(self):
        self.section.label_translations = {"en": "Care", "fr": "Soins", "ar": ""}
        self.section.save(update_fields=["label_translations"])

        french = self.client.get(
            "/api/catalog/sections/", HTTP_X_STORE_SLUG=self.store.slug, HTTP_ACCEPT_LANGUAGE="fr-MA",
        )
        arabic = self.client.get(
            "/api/catalog/sections/", HTTP_X_STORE_SLUG=self.store.slug, HTTP_ACCEPT_LANGUAGE="ar",
        )
        self.assertEqual(french.status_code, 200, french.content)
        self.assertIn("Accept-Language", french.headers["Vary"])
        self.assertEqual(french.json()[0]["label"], "Soins")
        self.assertEqual(arabic.json()[0]["label"], "Care")
        self.assertEqual(french.json()[0]["label_translations"], {"en": "Care", "fr": "Soins", "ar": ""})

        self.section.label = "عناية"
        self.section.label_translations = {"en": "", "fr": "", "ar": "عناية"}
        self.section.save(update_fields=["label", "label_translations"])
        available = self.client.get(
            "/api/catalog/sections/", HTTP_X_STORE_SLUG=self.store.slug, HTTP_ACCEPT_LANGUAGE="fr",
        )
        self.assertEqual(available.json()[0]["label"], "عناية")


class MerchantCatalogEntitlementTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="catalog-limit@example.com", password="pass")
        self.retail_type = RetailType.objects.get(key="general")
        self.store = Store.objects.create(
            name="Limited catalog",
            slug="limited-catalog",
            retail_type=self.retail_type,
        )
        StoreMember.objects.create(
            user=self.user,
            store=self.store,
            role=StoreMember.Roles.OWNER,
        )
        ensure_default_sections(self.store)
        self.section = self.store.catalog_sections.first()
        ensure_free_subscription(self.user)
        product_limit = EntitlementDefinition.objects.get(key="catalog.max_products")
        free_plan = SubscriptionPlan.objects.get(slug="free")
        PlanEntitlement.objects.update_or_create(
            plan=free_plan,
            definition=product_limit,
            defaults={"value": 1},
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_product_limit_applies_to_products_not_categories(self):
        Product.objects.create(
            store=self.store,
            section=self.section,
            name="Existing product",
            ref="EXISTING-1",
            price="10.00",
        )

        category_response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/sections/",
            {"key": "new-category", "label": "New category"},
            format="json",
        )
        self.assertEqual(category_response.status_code, 201, category_response.data)

        product_response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/products/",
            {
                "section": self.section.pk,
                "name": "One product too many",
                "ref": "LIMIT-2",
                "price": "20.00",
                "attributes": {},
                "extra": {},
            },
            format="json",
        )
        self.assertEqual(product_response.status_code, 403, product_response.data)
        self.assertEqual(self.store.products.count(), 1)

    def test_product_creation_succeeds_while_quota_has_room(self):
        response = self.client.post(
            f"/api/catalog/store/{self.store.pk}/products/",
            {
                "section": self.section.pk,
                "name": "First product",
                "ref": "FIRST-1",
                "price": "20.00",
                "attributes": {},
                "extra": {},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.store.products.count(), 1)


class MerchantBulkProductDeletionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="bulk-delete@example.com", password="pass")
        self.store = Store.objects.create(name="Bulk delete", slug="bulk-delete")
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        self.section = CatalogSection.objects.create(store=self.store, key="general", label="General")
        for index in range(2):
            Product.objects.create(store=self.store, section=self.section, ref=f"DEL-{index}", name=f"Delete {index}", price="1")
        self.other_store = Store.objects.create(name="Other", slug="bulk-delete-other")
        other_section = CatalogSection.objects.create(store=self.other_store, key="general", label="General")
        Product.objects.create(store=self.other_store, section=other_section, ref="KEEP", name="Keep", price="1")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.url = f"/api/catalog/store/{self.store.pk}/products/delete-all/"

    def test_exact_confirmation_and_count_are_required(self):
        wrong_phrase = self.client.delete(self.url, {
            "store_slug": self.store.slug, "confirmation": "delete products", "expected_count": 2,
        }, format="json")
        self.assertEqual(wrong_phrase.status_code, 400)
        stale_count = self.client.delete(self.url, {
            "store_slug": self.store.slug, "confirmation": "delete all products", "expected_count": 1,
        }, format="json")
        self.assertEqual(stale_count.status_code, 409)
        self.assertEqual(self.store.products.count(), 2)

    def test_confirmed_deletion_is_store_scoped(self):
        response = self.client.delete(self.url, {
            "store_slug": self.store.slug, "confirmation": "delete all products", "expected_count": 2,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["deleted"], 2)
        self.assertFalse(self.store.products.exists())
        self.assertEqual(self.other_store.products.count(), 1)

    def test_non_owner_cannot_bulk_delete(self):
        administrator = User.objects.create_user(email="bulk-admin@example.com", password="pass")
        StoreMember.objects.create(user=administrator, store=self.store, role=StoreMember.Roles.ADMIN)
        self.client.force_authenticate(administrator)
        response = self.client.delete(self.url, {
            "store_slug": self.store.slug, "confirmation": "delete all products", "expected_count": 2,
        }, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.products.count(), 2)

class ProductImageStorageTests(TestCase):
    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        },
    )
    def test_product_images_use_the_configured_default_storage(self):
        self.assertIsInstance(product_image_storage(), FileSystemStorage)

    def test_product_upload_rejects_spoofed_image_content(self):
        serializer = ProductImageWriteSerializer(data={
            "image": SimpleUploadedFile(
                "not-an-image.png",
                b"<script>alert('stored')</script>",
                content_type="image/png",
            ),
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("image", serializer.errors)

    def test_product_upload_rejects_svg_active_content(self):
        serializer = ProductImageWriteSerializer(data={
            "image": SimpleUploadedFile(
                "active.svg",
                b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                content_type="image/svg+xml",
            ),
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("image", serializer.errors)

    @override_settings(CLOUDINARY_STORAGE={
        "CLOUD_NAME": "test-cloud", "API_KEY": "test-key", "API_SECRET": "test-secret",
    }, STORAGES={
        "default": {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    @patch("common.media_uploads.verify_api_response_signature", return_value=True)
    @patch("common.media_uploads.cloudinary.api.resource", return_value={"format": "webp", "bytes": 1024, "version": 42})
    def test_verified_direct_upload_is_attached_without_reuploading_bytes(self, resource, verify_signature):
        store = Store.objects.create(name="Direct upload", slug="direct-upload")
        section = CatalogSection.objects.create(store=store, key="general", label="General")
        product = Product.objects.create(store=store, section=section, name="Item", ref="DIRECT-1", price="10")
        serializer = ProductImageWriteSerializer(data={
            "direct_upload": {
                "public_id": f"{image_folder(store.id, 'products', product.id)}/photo_abc",
                "version": 42,
                "signature": "cloudinary-response-signature",
                "resource_type": "image",
                "format": "webp",
                "bytes": 1024,
            },
        }, context={"store": store, "product": product})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        image = serializer.save(product=product)
        self.assertEqual(image.image.name, f"{image_folder(store.id, 'products', product.id)}/photo_abc")
        verify_signature.assert_called_once_with(image.image.name, 42, "cloudinary-response-signature")
        resource.assert_called_once_with(image.image.name, resource_type="image", type="upload")

    @override_settings(CLOUDINARY_STORAGE={
        "CLOUD_NAME": "test-cloud", "API_KEY": "test-key", "API_SECRET": "test-secret",
    }, STORAGES={
        "default": {"BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_signing_contract_has_no_secret_and_is_resource_scoped(self):
        payload = signed_upload_payload("tijarago/stores/7/products/11")
        self.assertTrue(payload["direct_upload"])
        self.assertNotIn("api_secret", payload)
        self.assertEqual(payload["params"]["folder"], "tijarago/stores/7/products/11")
        self.assertEqual(payload["resource_type"], "image")

    def test_category_thumbnail_rejects_spoofed_image_content(self):
        serializer = MerchantSectionSerializer(data={
            "key": "unsafe",
            "label": "Unsafe",
            "thumbnail": SimpleUploadedFile(
                "not-an-image.png",
                b"this is not a png",
                content_type="image/png",
            ),
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("thumbnail", serializer.errors)


class RetailTypePresetTests(TestCase):
    def test_all_supported_retail_types_are_installed(self):
        self.assertEqual(
            set(RetailType.objects.values_list("key", flat=True)),
            set(RETAIL_TYPE_SECTIONS),
        )

    def test_reference_restore_command_recovers_missing_retail_types(self):
        RetailType.objects.all().delete()

        call_command("restore_reference_data", verbosity=0)

        self.assertEqual(
            set(RetailType.objects.values_list("key", flat=True)),
            set(RETAIL_TYPE_SECTIONS),
        )

    def test_every_retail_type_builds_its_expected_catalog(self):
        for retail_type in RetailType.objects.all():
            with self.subTest(retail_type=retail_type.key):
                store = Store.objects.create(
                    name=retail_type.label,
                    slug=f"test-{retail_type.key}",
                    retail_type=retail_type,
                )
                created = ensure_default_sections(store)
                self.assertEqual(created, len(RETAIL_TYPE_SECTIONS[retail_type.key]))
                self.assertEqual(
                    list(store.catalog_sections.values_list("key", flat=True)),
                    RETAIL_TYPE_SECTIONS[retail_type.key],
                )
                for definition in store.attribute_defs.all():
                    self.assertIsInstance(definition.options, list)

    def test_retail_config_rejects_invalid_shapes(self):
        with self.assertRaises(ValidationError):
            validate_retail_type_config({"product_fields": [], "features": {"bad": "yes"}})

    def test_seeding_validates_presets_before_persisting_them(self):
        invalid = deepcopy(RETAIL_TYPE_PRESETS)
        invalid[0]["config"]["features"]["has_variants"] = "yes"
        with patch("catalog.seeds.RETAIL_TYPE_PRESETS", invalid):
            with self.assertRaises(ValidationError):
                ensure_retail_types()

    def test_books_seed_advertises_only_physical_formats(self):
        books = next(section for section in DEFAULT_SECTIONS if section["key"] == "books")
        formats = next(item for item in books["attribute_defs"] if item["key"] == "format")
        values = {option["value"] for option in formats["options"]}
        self.assertEqual(values, {"Hardcover", "Paperback"})


class ConfigDrivenSerializerTests(TestCase):
    def setUp(self):
        self.retail_type = RetailType.objects.get(key="clothing")
        self.store = Store.objects.create(
            name="Fashion Store", slug="fashion-store", retail_type=self.retail_type,
        )
        ensure_default_sections(self.store)
        self.section = self.store.catalog_sections.get(key="shoes")

    def test_product_extra_is_writable_validated_and_normalized(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.id,
                "name": "Walking Shoe",
                "ref": "SHOE-1",
                "price": "299.00",
                "attributes": {},
                "extra": {"sku": "SHOE-1", "weight": "1.5", "tags": "shoe, walking"},
            },
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save(store=self.store)
        self.assertEqual(product.extra["weight"], 1.5)
        self.assertEqual(product.extra["tags"], ["shoe", "walking"])

    def test_variant_extra_is_writable_and_number_attributes_are_normalized(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Walking Shoe", ref="SHOE-2", price=299,
            uses_variants=True,
        )
        serializer = MerchantVariantSerializer(
            data={
                "attributes": {"size": "42", "color": "Black"},
                "price": "319.00",
                "quantity": 5,
                "extra": {"sku": "SHOE-2-BLACK-42"},
            },
            context={"store": self.store, "product": product},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        variant = serializer.save(store=self.store, product=product)
        self.assertEqual(variant.attributes["size"], 42)
        self.assertEqual(variant.extra["sku"], "SHOE-2-BLACK-42")

    def test_clothing_product_chooses_its_sizing_system(self):
        section = self.store.catalog_sections.get(key="men")
        product_serializer = MerchantProductSerializer(
            data={
                "section": section.id,
                "name": "Everyday Shirt",
                "ref": "SHIRT-1",
                "price": "199.00",
                "uses_variants": True,
                "attributes": {"size_system": "letters"},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(product_serializer.is_valid(), product_serializer.errors)
        product = product_serializer.save(store=self.store)

        variant_serializer = MerchantVariantSerializer(
            data={"attributes": {"size": "xl", "color": "Black"}, "quantity": 3, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(variant_serializer.is_valid(), variant_serializer.errors)
        self.assertEqual(variant_serializer.validated_data["attributes"]["size"], "XL")

        numeric_size = MerchantVariantSerializer(
            data={"attributes": {"size": "42", "color": "Black"}, "quantity": 3, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertFalse(numeric_size.is_valid())
        self.assertIn("size", str(numeric_size.errors))

    def test_numeric_clothing_sizes_are_canonical_and_policy_is_locked_with_variants(self):
        section = self.store.catalog_sections.get(key="women")
        product = Product.objects.create(
            store=self.store,
            section=section,
            name="Tailored Trousers",
            ref="TROUSER-1",
            price=399,
            attributes={"size_system": "numeric"},
            uses_variants=True,
        )
        variant_serializer = MerchantVariantSerializer(
            data={"attributes": {"size": "036.50", "color": "Black"}, "quantity": 2, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(variant_serializer.is_valid(), variant_serializer.errors)
        variant = variant_serializer.save(store=self.store, product=product)
        self.assertEqual(variant.attributes["size"], "36.5")

        product_serializer = MerchantProductSerializer(
            product,
            data={"attributes": {"size_system": "letters"}},
            partial=True,
            context={"store": self.store},
        )
        self.assertFalse(product_serializer.is_valid())
        self.assertIn("size_system", str(product_serializer.errors))

    def test_sizing_migration_backfills_existing_clothing_products(self):
        section = self.store.catalog_sections.get(key="men")
        section.attribute_defs.filter(key="size_system").delete()
        section.attribute_defs.filter(key="size").update(
            type="select",
            options=[{"value": "42", "label": "42"}],
        )
        product = Product.objects.create(
            store=self.store,
            section=section,
            name="Legacy Numeric Product",
            ref="LEGACY-SIZE-1",
            price=100,
            attributes={},
        )
        StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"size": 42, "color": "Black"},
            quantity=1,
        )

        migration = importlib.import_module("catalog.migrations.0009_clothing_product_sizing")
        migration.add_product_sizing(django_apps, None)

        product.refresh_from_db()
        self.assertEqual(product.attributes["size_system"], "numeric")
        self.assertEqual(section.attribute_defs.get(key="size").type, "text")
        sizing_definition = AttributeDef.objects.get(section=section, key="size_system")
        self.assertTrue(sizing_definition.required)

    def test_unknown_extra_field_is_rejected(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.id,
                "name": "Walking Shoe",
                "ref": "SHOE-3",
                "price": "299.00",
                "attributes": {},
                "extra": {"expiry_date": "2030-01-01"},
            },
            context={"store": self.store},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("expiry_date", str(serializer.errors))

    def test_duplicate_variant_combination_and_sku_are_rejected(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Walking Shoe", ref="SHOE-5", price=299,
        )
        StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"size": 42, "color": "Black"},
            quantity=5,
            extra={"sku": "DUPLICATE-SKU"},
        )
        duplicate_options = MerchantVariantSerializer(
            data={
                "attributes": {"size": "42", "color": "Black"},
                "quantity": 2,
                "extra": {"sku": "ANOTHER-SKU"},
            },
            context={"store": self.store, "product": product},
        )
        self.assertFalse(duplicate_options.is_valid())
        self.assertIn("attributes", duplicate_options.errors)

        duplicate_sku = MerchantVariantSerializer(
            data={
                "attributes": {"size": "43", "color": "Black"},
                "quantity": 2,
                "extra": {"sku": "DUPLICATE-SKU"},
            },
            context={"store": self.store, "product": product},
        )
        self.assertFalse(duplicate_sku.is_valid())
        self.assertIn("extra", duplicate_sku.errors)

    def test_reserved_stock_cannot_exceed_quantity(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Walking Shoe", ref="SHOE-4", price=299,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockVariant.objects.create(
                store=self.store,
                product=product,
                attributes={"size": 42, "color": "Black"},
                quantity=1,
                reserved=2,
            )

    def test_product_with_variants_cannot_move_to_an_incompatible_section(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Walking Shoe", ref="SHOE-6", price=299,
        )
        StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"size": 42, "color": "Black"},
            quantity=5,
        )
        other_section = self.store.catalog_sections.get(key="men")
        serializer = MerchantProductSerializer(
            product,
            data={"section": other_section.id},
            partial=True,
            context={"store": self.store},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("section", serializer.errors)

    def test_simple_product_receives_default_inventory_variant(self):
        section = self.store.catalog_sections.get(key="hygiene")
        serializer = MerchantProductSerializer(
            data={
                "section": section.id,
                "name": "Simple Soap",
                "ref": "SOAP-SIMPLE-1",
                "price": "25.00",
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save(store=self.store)

        self.assertTrue(product.is_active)
        variant = product.variants.get()
        self.assertEqual(variant.attributes, {})
        self.assertEqual(variant.quantity, 0)

    def test_default_inventory_does_not_lock_product_section(self):
        simple_section = self.store.catalog_sections.get(key="hygiene")
        product_serializer = MerchantProductSerializer(
            data={
                "section": simple_section.id,
                "name": "Reclassified Product",
                "ref": "RECLASSIFY-1",
                "price": "25.00",
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(product_serializer.is_valid(), product_serializer.errors)
        product = product_serializer.save(store=self.store)
        self.assertEqual(product.variants.count(), 1)

        changed = MerchantProductSerializer(
            product,
            data={"section": self.section.id, "attributes": {}},
            partial=True,
            context={"store": self.store},
        )
        self.assertTrue(changed.is_valid(), changed.errors)
        product = changed.save()

        self.assertEqual(product.section_id, self.section.id)
        self.assertTrue(product.is_active)
        self.assertEqual(product.variants.count(), 1)
        self.assertEqual(product.variants.get().attributes, {})

    def test_configurable_product_stays_draft_until_a_variant_exists(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.id,
                "name": "Configurable Shoe",
                "ref": "SHOE-DRAFT-1",
                "price": "299.00",
                "uses_variants": True,
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save(store=self.store)
        self.assertFalse(product.is_active)
        self.assertFalse(product.variants.exists())

        premature_activation = MerchantProductSerializer(
            product,
            data={"is_active": True},
            partial=True,
            context={"store": self.store},
        )
        self.assertFalse(premature_activation.is_valid())
        self.assertIn("is_active", premature_activation.errors)

        variant = MerchantVariantSerializer(
            data={"attributes": {"size": "42", "color": "Black"}, "quantity": 2, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(variant.is_valid(), variant.errors)
        variant.save(store=self.store, product=product)
        activation = MerchantProductSerializer(
            product,
            data={"is_active": True},
            partial=True,
            context={"store": self.store},
        )
        self.assertTrue(activation.is_valid(), activation.errors)
        self.assertTrue(activation.save().is_active)

    def test_in_use_attribute_schema_cannot_be_changed_or_removed(self):
        definition = self.section.attribute_defs.get(key="color")
        product = Product.objects.create(
            store=self.store, section=self.section, name="Protected Shoe", ref="SHOE-SCHEMA-1", price=299,
        )
        StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"size": 42, "color": "Black"},
            quantity=1,
        )
        changed = MerchantAttributeDefSerializer(
            definition,
            data={"options": [{"value": "White", "label": "White"}]},
            partial=True,
        )
        self.assertFalse(changed.is_valid())
        self.assertIn("schema", changed.errors)

        definitions = [
            {
                "key": item.key,
                "label": item.label,
                "type": item.type,
                "level": item.level,
                "options": item.options,
                "required": item.required,
                "sort_order": item.sort_order,
            }
            for item in self.section.attribute_defs.exclude(key="color")
        ]
        section_update = MerchantSectionSerializer(
            self.section,
            data={"attribute_defs": definitions},
            partial=True,
        )
        self.assertFalse(section_update.is_valid())
        self.assertIn("color", str(section_update.errors))

    def test_required_attribute_cannot_be_added_over_existing_records(self):
        definition = self.section.attribute_defs.get(key="size")
        definition.required = False
        definition.save(update_fields=["required"])
        product = Product.objects.create(
            store=self.store, section=self.section, name="Existing Shoe", ref="SHOE-EXISTING-1", price=299,
        )
        StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"color": "Black"},
            quantity=1,
        )
        changed = MerchantAttributeDefSerializer(
            definition,
            data={"required": True},
            partial=True,
        )
        self.assertFalse(changed.is_valid())
        self.assertIn("required", changed.errors)

    def test_active_product_last_variant_and_used_attribute_cannot_be_deleted(self):
        user = User.objects.create_user(email="catalog-owner@example.com", password="pass")
        StoreMember.objects.create(user=user, store=self.store, role=StoreMember.Roles.OWNER)
        product = Product.objects.create(
            store=self.store, section=self.section, name="Deletion Guard", ref="SHOE-DELETE-1", price=299,
        )
        variant = StockVariant.objects.create(
            store=self.store,
            product=product,
            attributes={"size": 42, "color": "Black"},
            quantity=1,
        )
        product.is_active = True
        product.save(update_fields=["is_active"])
        client = APIClient()
        client.force_authenticate(user)

        variant_response = client.delete(
            f"/api/catalog/store/{self.store.pk}/products/{product.pk}/variants/{variant.pk}/"
        )
        self.assertEqual(variant_response.status_code, 400)
        self.assertTrue(StockVariant.objects.filter(pk=variant.pk).exists())

        color = self.section.attribute_defs.get(key="color")
        attribute_response = client.delete(
            f"/api/catalog/store/{self.store.pk}/sections/{self.section.pk}/attributes/{color.pk}/"
        )
        self.assertEqual(attribute_response.status_code, 400)
        self.assertTrue(AttributeDef.objects.filter(pk=color.pk).exists())

    def test_inventory_migration_repairs_legacy_products_and_book_formats(self):
        simple_section = self.store.catalog_sections.get(key="hygiene")
        simple = Product.objects.create(
            store=self.store, section=simple_section, name="Legacy Soap", ref="LEGACY-SOAP-1", price=20,
        )
        configurable = Product.objects.create(
            store=self.store, section=self.section, name="Legacy Shoe", ref="LEGACY-SHOE-2", price=200,
        )
        books_type = RetailType.objects.get(key="books")
        books_store = Store.objects.create(name="Books", slug="legacy-books", retail_type=books_type)
        ensure_default_sections(books_store)
        format_definition = books_store.attribute_defs.get(key="format")
        format_definition.options += [
            {"value": "eBook", "label": "eBook"},
            {"value": "Audiobook", "label": "Audiobook"},
        ]
        format_definition.save(update_fields=["options"])

        migration = importlib.import_module("catalog.migrations.0010_harden_product_inventory")
        migration.harden_existing_catalog(django_apps, None)

        simple.refresh_from_db(); configurable.refresh_from_db(); format_definition.refresh_from_db()
        self.assertEqual(simple.variants.count(), 1)
        self.assertTrue(simple.is_active)
        self.assertFalse(configurable.is_active)
        self.assertNotIn("eBook", {item["value"] for item in format_definition.options})
        self.assertNotIn("Audiobook", {item["value"] for item in format_definition.options})


class CommerceProductContractTests(TestCase):
    def setUp(self):
        self.retail_type = RetailType.objects.get(key="general")
        self.store = Store.objects.create(name="Commerce", slug="commerce", retail_type=self.retail_type)
        ensure_default_sections(self.store)
        self.section = self.store.catalog_sections.first()

    def test_product_has_stable_slug_pricing_taxonomy_and_relations(self):
        vendor = ProductVendor.objects.create(store=self.store, name="Atlas Works")
        related = Product.objects.create(
            store=self.store, section=self.section, name="Related item", ref="RELATED-1", price="20.00",
        )
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "category_ids": [self.section.pk],
                "vendor_ids": [vendor.pk],
                "related_product_ids": [related.pk],
                "name": "Everyday Bag",
                "ref": "BAG-1",
                "price": "80.00",
                "compare_at_price": "100.00",
                "cost_price": "45.00",
                "tags": ["bags", " everyday "],
                "seo_title": "Everyday Bag",
                "seo_description": "A practical everyday bag.",
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save(store=self.store)

        self.assertEqual(product.slug, "everyday-bag")
        self.assertEqual(product.you_save_amount, 20)
        self.assertEqual(product.tags, ["bags", "everyday"])
        self.assertEqual(list(product.categories.all()), [self.section])
        self.assertEqual(list(product.vendors.all()), [vendor])
        self.assertEqual(list(product.related_products.all()), [related])
        self.assertNotIn("cost_price", ProductSerializer(product).data)

    def test_product_description_keeps_supported_formatting_and_removes_unsafe_markup(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Formatted product",
                "ref": "FORMATTED-1",
                "price": "80.00",
                "description": (
                    '<h2 style="color:red">Details</h2><p>Useful <strong>copy</strong> '
                    '<a href="javascript:alert(1)" onclick="alert(1)">here</a>.</p>'
                    '<script>alert("unsafe")</script><iframe src="https://example.com"></iframe>'
                ),
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save(store=self.store)
        self.assertEqual(
            product.description,
            "<h2>Details</h2><p>Useful <strong>copy</strong> <a>here</a>.</p>",
        )
        self.assertNotIn("javascript", product.description)
        self.assertNotIn("alert", product.description)

    def test_duplicate_product_slug_and_cross_store_taxonomy_are_rejected(self):
        Product.objects.create(
            store=self.store, section=self.section, name="First", slug="same-url", ref="FIRST", price=10,
        )
        other = Store.objects.create(name="Other", slug="other-commerce", retail_type=self.retail_type)
        ensure_default_sections(other)
        duplicate_slug = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Second",
                "slug": "same-url",
                "ref": "SECOND",
                "price": "20.00",
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertFalse(duplicate_slug.is_valid())
        self.assertIn("slug", duplicate_slug.errors)
        foreign_category = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "category_ids": [other.catalog_sections.first().pk],
                "name": "Third",
                "ref": "THIRD",
                "price": "20.00",
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertFalse(foreign_category.is_valid())
        self.assertIn("category_ids", foreign_category.errors)

    def test_model_relations_reject_cross_store_and_self_links(self):
        other = Store.objects.create(name="Other relation", slug="other-relation", retail_type=self.retail_type)
        ensure_default_sections(other)
        other_section = other.catalog_sections.first()
        product = Product.objects.create(
            store=self.store, section=self.section, name="Safe relation", ref="SAFE-REL", price=10,
        )
        other_product = Product.objects.create(
            store=other, section=other_section, name="Foreign relation", ref="FOREIGN-REL", price=10,
        )

        with self.assertRaises(ValidationError), transaction.atomic():
            product.categories.add(other_section)
        with self.assertRaises(ValidationError), transaction.atomic():
            product.related_products.add(other_product)
        with self.assertRaises(ValidationError), transaction.atomic():
            product.related_products.add(product)
        with self.assertRaises(ValidationError):
            StockVariant.objects.create(
                store=self.store,
                product=other_product,
                attributes={},
                quantity=1,
            )

    def test_variant_sku_barcode_and_image_are_product_scoped(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Item", ref="ITEM-1", price=30,
            uses_variants=True,
        )
        first = MerchantVariantSerializer(
            data={"attributes": {}, "quantity": 2, "sku": "SKU-1", "barcode": "6110001", "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(first.is_valid(), first.errors)
        first.save(store=self.store, product=product)
        other_product = Product.objects.create(
            store=self.store, section=self.section, name="Other item", ref="ITEM-2", price=30,
            uses_variants=True,
        )
        duplicate = MerchantVariantSerializer(
            data={"attributes": {}, "quantity": 2, "sku": "SKU-1", "extra": {}},
            context={"store": self.store, "product": other_product},
        )
        self.assertFalse(duplicate.is_valid())
        self.assertIn("sku", duplicate.errors)

    def test_variant_mode_is_explicit_and_disabling_requires_confirmation(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Configurable", ref="CONFIG-1",
            price=30, uses_variants=True, is_active=False,
        )
        variant = StockVariant.objects.create(
            store=self.store, product=product, attributes={"size": "M"}, quantity=4, reserved=1,
        )

        unconfirmed = MerchantProductSerializer(
            product,
            data={"uses_variants": False},
            partial=True,
            context={"store": self.store},
        )
        self.assertFalse(unconfirmed.is_valid())
        self.assertIn("uses_variants", unconfirmed.errors)

        confirmed = MerchantProductSerializer(
            product,
            data={"uses_variants": False, "clear_variants": True},
            partial=True,
            context={"store": self.store},
        )
        self.assertFalse(confirmed.is_valid())
        self.assertIn("reserved stock", str(confirmed.errors))

        variant.reserved = 0
        variant.save(update_fields=["reserved"])
        confirmed = MerchantProductSerializer(
            product,
            data={"uses_variants": False, "clear_variants": True},
            partial=True,
            context={"store": self.store},
        )
        self.assertTrue(confirmed.is_valid(), confirmed.errors)
        product = confirmed.save()
        self.assertFalse(product.uses_variants)
        self.assertEqual(product.variants.count(), 1)
        self.assertEqual(product.variants.get().attributes, {})

    def test_related_product_visibility_is_an_explicit_public_choice(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Recommendations", ref="RELATED-SWITCH",
            price=30, show_related_products=True,
        )
        data = ProductSerializer(product).data
        self.assertTrue(data["show_related_products"])
        self.assertFalse(data["uses_variants"])

    def test_product_owned_choices_validate_variant_combinations(self):
        product_serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Flexible Shirt",
                "ref": "FLEX-SHIRT",
                "price": "80.00",
                "uses_variants": True,
                "is_active": False,
                "variant_options": [
                    {"key": "color", "label": "Color", "type": "color", "values": ["Black", "White"]},
                    {"key": "size", "label": "Size", "type": "select", "values": ["S", "M"]},
                ],
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(product_serializer.is_valid(), product_serializer.errors)
        product = product_serializer.save(store=self.store)

        valid = MerchantVariantSerializer(
            data={"attributes": {"color": "White", "size": "M"}, "quantity": 3, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        valid.save(store=self.store, product=product)

        invalid = MerchantVariantSerializer(
            data={"attributes": {"color": "Blue", "size": "M"}, "quantity": 3, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("Black, White", str(invalid.errors))

    def test_product_choice_values_are_case_insensitively_unique(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Duplicate sizes",
                "ref": "DUPLICATE-SIZES",
                "price": "80.00",
                "uses_variants": True,
                "is_active": False,
                "variant_options": [
                    {"key": "size", "label": "Size", "type": "select", "values": ["XL", "xl"]},
                ],
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("same value", str(serializer.errors))

    def test_custom_color_choices_preserve_safe_swatch_metadata(self):
        product_serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Custom colour shirt",
                "ref": "CUSTOM-COLOUR",
                "price": "80.00",
                "uses_variants": True,
                "is_active": False,
                "variant_options": [{
                    "key": "color",
                    "label": "Color",
                    "type": "color",
                    "values": [
                        {"value": "Ocean blue", "label": "Ocean blue", "swatch": "#0E7490"},
                        "White",
                    ],
                }],
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertTrue(product_serializer.is_valid(), product_serializer.errors)
        product = product_serializer.save(store=self.store)
        self.assertEqual(
            product.variant_options[0]["values"][0],
            {"value": "Ocean blue", "label": "Ocean blue", "swatch": "#0e7490"},
        )

        variant = MerchantVariantSerializer(
            data={"attributes": {"color": "ocean BLUE"}, "quantity": 3, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(variant.is_valid(), variant.errors)
        self.assertEqual(variant.validated_data["attributes"], {"color": "Ocean blue"})

    def test_custom_color_choice_rejects_invalid_swatch(self):
        serializer = MerchantProductSerializer(
            data={
                "section": self.section.pk,
                "name": "Unsafe colour",
                "ref": "UNSAFE-COLOUR",
                "price": "80.00",
                "uses_variants": True,
                "is_active": False,
                "variant_options": [{
                    "key": "color",
                    "label": "Color",
                    "type": "color",
                    "values": [{"value": "Unsafe", "label": "Unsafe", "swatch": "red; background:url(x)"}],
                }],
                "attributes": {},
                "extra": {},
            },
            context={"store": self.store},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("six-digit color", str(serializer.errors))

    def test_variant_choice_input_is_saved_with_the_configured_casing(self):
        product = Product.objects.create(
            store=self.store,
            section=self.section,
            name="Canonical sizes",
            ref="CANONICAL-SIZES",
            price=80,
            uses_variants=True,
            is_active=False,
            variant_options=[
                {"key": "size", "label": "Size", "type": "select", "values": ["XL"]},
            ],
        )
        serializer = MerchantVariantSerializer(
            data={"attributes": {"size": "xl"}, "quantity": 2, "extra": {}},
            context={"store": self.store, "product": product},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        variant = serializer.save(store=self.store, product=product)
        self.assertEqual(variant.attributes, {"size": "XL"})

    def test_disabling_choices_clears_the_product_choice_schema(self):
        product = Product.objects.create(
            store=self.store,
            section=self.section,
            name="Simple later",
            ref="SIMPLE-LATER",
            price=80,
            uses_variants=True,
            is_active=False,
            variant_options=[
                {"key": "size", "label": "Size", "type": "select", "values": ["M"]},
            ],
        )
        serializer = MerchantProductSerializer(
            product,
            data={"uses_variants": False},
            partial=True,
            context={"store": self.store},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        product = serializer.save()
        self.assertEqual(product.variant_options, [])

    def test_published_reviews_drive_product_summary(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Reviewed", ref="REVIEW-1", price=30,
        )
        first = ProductReview.objects.create(
            store=self.store, product=product, customer_name="One", rating=4,
            status=ProductReview.Status.PUBLISHED,
        )
        ProductReview.objects.create(
            store=self.store, product=product, customer_name="Two", rating=2,
            status=ProductReview.Status.PENDING,
        )
        product.refresh_from_db()
        self.assertEqual(product.reviews_count, 1)
        self.assertEqual(product.rating, 4)
        first.delete()
        product.refresh_from_db()
        self.assertEqual(product.reviews_count, 0)
        self.assertEqual(product.rating, 0)

    def test_public_product_detail_uses_store_scoped_slug(self):
        product = Product.objects.create(
            store=self.store, section=self.section, name="Public Item", ref="PUBLIC-1", price=30,
        )
        response = APIClient().get(
            f"/api/catalog/products/{product.slug}/",
            HTTP_X_STORE_SLUG=self.store.slug,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], product.pk)
        self.assertNotIn("cost_price", response.data)
