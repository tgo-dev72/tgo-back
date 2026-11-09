from django.db import migrations, models


def backfill_tax_settings(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    OrderedProduct = apps.get_model("orders", "OrderedProduct")
    StoreConfig = apps.get_model("store", "StoreConfig")
    configs = {
        config.store_id: config
        for config in StoreConfig.objects.all().iterator()
    }
    for order in Order.objects.all().iterator():
        line_rate = (
            OrderedProduct.objects.filter(order_id=order.pk, tax_rate__isnull=False)
            .values_list("tax_rate", flat=True)
            .first()
        )
        config = configs.get(order.store_id)
        order.tax_rate = config.invoice_tax_rate if config and config.invoice_tax_rate is not None else line_rate
        order.prices_include_tax = config.invoice_prices_include_tax if config else True
        order.save(update_fields=["tax_rate", "prices_include_tax"])


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0011_order_tax_amount_order_tax_breakdown_and_more"),
        ("store", "0030_storeconfig_invoice_tax_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="tax_rate",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="prices_include_tax",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(backfill_tax_settings, migrations.RunPython.noop),
    ]
