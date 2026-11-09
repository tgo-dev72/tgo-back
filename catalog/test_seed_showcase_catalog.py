from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase, override_settings

from catalog.management.commands.seed_showcase_catalog import PRODUCTS, SHOWCASE_PREFIX
from catalog.models import CatalogSection, Product, StockVariant
from store.models import Store


class SeedShowcaseCatalogTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="LUX", slug="lux")
        CatalogSection.objects.create(store=self.store, key="products", label="Products")

    @override_settings(MEDIA_ROOT="/tmp/tijarago-showcase-command-test")
    def test_seed_is_idempotent_and_remove_only_deletes_showcase_products(self):
        merchant_product = Product.objects.create(
            store=self.store,
            section=self.store.catalog_sections.get(key="products"),
            name="Merchant product",
            ref="MERCHANT-001",
            price="100.00",
        )

        with TemporaryDirectory() as directory:
            media_dir = Path(directory)
            for item in PRODUCTS:
                (media_dir / item["image"]).write_bytes(b"showcase-image")

            call_command("seed_showcase_catalog", store="lux", media_dir=media_dir)
            call_command("seed_showcase_catalog", store="lux", media_dir=media_dir)

        products = Product.objects.filter(store=self.store, ref__startswith=SHOWCASE_PREFIX)
        self.assertEqual(products.count(), len(PRODUCTS))
        self.assertEqual(StockVariant.objects.filter(product__in=products).count(), 44)
        self.assertTrue(all(product.uses_variants for product in products))
        self.assertTrue(all(product.images.count() == 1 for product in products))

        call_command("seed_showcase_catalog", store="lux", remove=True)
        self.assertFalse(Product.objects.filter(store=self.store, ref__startswith=SHOWCASE_PREFIX).exists())
        self.assertTrue(Product.objects.filter(pk=merchant_product.pk).exists())
