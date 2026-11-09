from django.db import migrations, models


def infer_product_choices(apps, schema_editor):
    Product = apps.get_model("catalog", "Product")
    database = schema_editor.connection.alias

    for product in Product.objects.using(database).iterator():
        variants = product.variants.using(database).all()
        uses_variants = variants.count() > 1 or variants.exclude(attributes={}).exists()
        show_related_products = product.related_products.using(database).exists()
        Product.objects.using(database).filter(pk=product.pk).update(
            uses_variants=uses_variants,
            show_related_products=show_related_products,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0012_commerce_product_model"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="variant_options",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Product-specific customer choices such as color, size or material.",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="uses_variants",
            field=models.BooleanField(
                default=False,
                help_text="Merchant choice: customers select an option combination for this product.",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="show_related_products",
            field=models.BooleanField(
                default=False,
                help_text="Merchant choice: show recommendations on this product page.",
            ),
        ),
        migrations.RunPython(infer_product_choices, migrations.RunPython.noop),
    ]
