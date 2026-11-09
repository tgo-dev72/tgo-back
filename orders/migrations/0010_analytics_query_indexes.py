from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0009_order_coupon_snapshot"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["store", "created_at"], name="order_store_created"),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["store", "status", "created_at"], name="order_store_status_time"),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["store", "payment_mode", "is_paid"], name="order_store_payment"),
        ),
        migrations.AddIndex(
            model_name="orderedproduct",
            index=models.Index(fields=["store", "product_id"], name="order_line_store_product"),
        ),
    ]

