from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0024_product_tax_rate"),
        ("store", "0030_storeconfig_invoice_tax_rate"),
    ]

    operations = [
        migrations.RemoveField(model_name="product", name="tax_rate"),
    ]
