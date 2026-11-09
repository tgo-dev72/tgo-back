from django.db import migrations, models


def rename_purchase_events(apps, schema_editor):
    StorefrontEvent = apps.get_model("commerce_analytics", "StorefrontEvent")
    StorefrontEvent.objects.filter(event_type="purchase").update(event_type="order_submitted")


class Migration(migrations.Migration):
    dependencies = [("commerce_analytics", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="storefrontevent",
            name="session_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(rename_purchase_events, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="storefrontevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("page_view", "Page view"),
                    ("product_view", "Product view"),
                    ("search", "Search"),
                    ("add_to_cart", "Add to cart"),
                    ("checkout_started", "Checkout started"),
                    ("order_submitted", "Order submitted"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="storefrontevent",
            index=models.Index(
                fields=["store", "session_hash", "occurred_at"],
                name="analytics_store_session",
            ),
        ),
    ]
