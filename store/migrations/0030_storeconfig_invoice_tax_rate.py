from decimal import Decimal

from django.db import migrations, models
import django.core.validators


def backfill_central_tax_rate(apps, schema_editor):
    StoreConfig = apps.get_model("store", "StoreConfig")
    Product = apps.get_model("catalog", "Product")
    for config in StoreConfig.objects.all().iterator():
        if config.invoice_tax_regime == "exempt":
            config.invoice_tax_rate = Decimal("0.00")
        elif config.invoice_tax_regime == "product":
            rates = list(
                Product.objects.filter(store_id=config.store_id, is_active=True)
                .exclude(tax_rate__isnull=True)
                .values_list("tax_rate", flat=True)
                .distinct()[:2]
            )
            missing = Product.objects.filter(store_id=config.store_id, is_active=True, tax_rate__isnull=True).exists()
            if len(rates) == 1 and not missing:
                config.invoice_tax_rate = rates[0]
        config.save(update_fields=["invoice_tax_rate"])


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0024_product_tax_rate"),
        ("store", "0029_storeconfig_free_shipping_currency_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="invoice_tax_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Merchant-declared store-wide tax percentage. Zero means tax exempt.",
                max_digits=5,
                null=True,
                validators=[django.core.validators.MinValueValidator(Decimal("0.00")), django.core.validators.MaxValueValidator(Decimal("100.00"))],
            ),
        ),
        migrations.RunPython(backfill_central_tax_rate, migrations.RunPython.noop),
    ]
