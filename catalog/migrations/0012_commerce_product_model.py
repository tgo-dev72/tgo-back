from decimal import Decimal, InvalidOperation

from django.db import migrations, models
import django.db.models.deletion
import django.core.validators
import django.utils.timezone

import catalog.models


def _unique_slug(Product, store_id, name, product_id):
    from django.utils.text import slugify

    base = slugify(name)[:240] or "product"
    candidate = base
    suffix = 2
    while Product.objects.filter(store_id=store_id, slug=candidate).exclude(pk=product_id).exists():
        tail = f"-{suffix}"
        candidate = f"{base[:255 - len(tail)]}{tail}"
        suffix += 1
    return candidate


def promote_existing_product_data(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    StockVariant = apps.get_model("catalog", "StockVariant")

    for product in Product.objects.all().iterator():
        extra = product.extra if isinstance(product.extra, dict) else {}
        updates = {
            "slug": _unique_slug(Product, product.store_id, product.name, product.pk),
            "tags": extra.get("tags", []) if isinstance(extra.get("tags", []), list) else [],
            "seo_title": str(extra.get("meta_title", "") or "")[:255],
            "seo_description": str(extra.get("meta_description", "") or ""),
        }
        Product.objects.filter(pk=product.pk).update(**updates)
        product.categories.add(product.section_id)

    seen_skus = set()
    seen_barcodes = set()
    for variant in StockVariant.objects.order_by("id").iterator():
        extra = variant.extra if isinstance(variant.extra, dict) else {}
        weight = extra.get("weight")
        try:
            weight = Decimal(str(weight)) if weight not in (None, "") else None
        except (InvalidOperation, TypeError, ValueError):
            weight = None
        sku = str(extra.get("sku", "") or "")[:100]
        barcode = str(extra.get("barcode", "") or "")[:100]
        sku_key = (variant.store_id, sku)
        barcode_key = (variant.store_id, barcode)
        if not sku or sku_key in seen_skus:
            sku = ""
        else:
            seen_skus.add(sku_key)
        if not barcode or barcode_key in seen_barcodes:
            barcode = ""
        else:
            seen_barcodes.add(barcode_key)
        StockVariant.objects.filter(pk=variant.pk).update(
            sku=sku,
            barcode=barcode,
            weight=weight,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0011_restore_retail_type_reference_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="catalogsection",
            name="description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="catalogsection",
            name="is_visible",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="catalogsection",
            name="parent",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="children", to="catalog.catalogsection"),
        ),
        migrations.AddField(
            model_name="catalogsection",
            name="sort_mode",
            field=models.CharField(choices=[("manual", "Manual"), ("newest", "Newest first"), ("price_asc", "Price: low to high"), ("price_desc", "Price: high to low"), ("name_asc", "Name: A to Z")], default="manual", max_length=16),
        ),
        migrations.AddField(
            model_name="catalogsection",
            name="thumbnail",
            field=models.FileField(blank=True, default="", storage=catalog.models.product_image_storage, upload_to="documents/categories"),
        ),
        migrations.CreateModel(
            name="ProductVendor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("slug", models.SlugField(max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_vendors", to="store.store")),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddConstraint(
            model_name="productvendor",
            constraint=models.UniqueConstraint(fields=("store", "slug"), name="uniq_product_vendor_slug_per_store"),
        ),
        migrations.AddField(
            model_name="product",
            name="compare_at_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))]),
        ),
        migrations.AddField(
            model_name="product",
            name="cost_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))]),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_description",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="product",
            name="seo_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="product",
            name="slug",
            field=models.SlugField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="product",
            name="tags",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="product",
            name="track_inventory",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="product",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AddField(
            model_name="product",
            name="categories",
            field=models.ManyToManyField(blank=True, help_text="Additional storefront categories; section remains the primary schema category.", related_name="catalog_products", to="catalog.catalogsection"),
        ),
        migrations.AddField(
            model_name="product",
            name="related_products",
            field=models.ManyToManyField(blank=True, related_name="recommended_by", symmetrical=False, to="catalog.product"),
        ),
        migrations.AddField(
            model_name="product",
            name="vendors",
            field=models.ManyToManyField(blank=True, related_name="products", to="catalog.productvendor"),
        ),
        migrations.AddField(
            model_name="productimage",
            name="alt_text",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="productimage",
            name="external_url",
            field=models.URLField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="productimage",
            name="media_type",
            field=models.CharField(choices=[("image", "Image"), ("external_video", "External video")], default="image", max_length=16),
        ),
        migrations.AlterField(
            model_name="productimage",
            name="image",
            field=models.FileField(blank=True, storage=catalog.models.product_image_storage, upload_to="documents/products"),
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="barcode",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="image",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="variants", to="catalog.productimage"),
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="sku",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="stockvariant",
            name="weight",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0.000"))]),
        ),
        migrations.RunPython(promote_existing_product_data, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.UniqueConstraint(condition=~models.Q(slug=""), fields=("store", "slug"), name="unique_product_slug_per_store"),
        ),
        migrations.AddConstraint(
            model_name="stockvariant",
            constraint=models.UniqueConstraint(condition=~models.Q(sku=""), fields=("store", "sku"), name="uniq_variant_sku_per_store"),
        ),
        migrations.AddConstraint(
            model_name="stockvariant",
            constraint=models.UniqueConstraint(condition=~models.Q(barcode=""), fields=("store", "barcode"), name="uniq_variant_barcode_per_store"),
        ),
        migrations.CreateModel(
            name="ProductReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("customer_name", models.CharField(max_length=100)),
                ("customer_email", models.EmailField(blank=True, default="", max_length=254)),
                ("rating", models.PositiveSmallIntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(5)])),
                ("title", models.CharField(blank=True, default="", max_length=160)),
                ("content", models.TextField(blank=True, default="")),
                ("status", models.CharField(choices=[("pending", "Pending"), ("published", "Published"), ("rejected", "Rejected")], default="pending", max_length=12)),
                ("verified_purchase", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="review_entries", to="catalog.product")),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_reviews", to="store.store")),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
