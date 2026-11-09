import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("catalog", "0013_product_merchandising_choices"),
        ("orders", "0010_analytics_query_indexes"),
        ("store", "0018_clientprofile_phone_verified_at_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="InventorySnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("snapshot_date", models.DateField()),
                ("quantity", models.PositiveIntegerField(default=0)),
                ("reserved", models.PositiveIntegerField(default=0)),
                ("available", models.PositiveIntegerField(default=0)),
                ("unit_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("unit_cost", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inventory_snapshots", to="catalog.product")),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inventory_snapshots", to="store.store")),
                ("variant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inventory_snapshots", to="catalog.stockvariant")),
            ],
            options={
                "ordering": ["-snapshot_date", "variant_id"],
                "indexes": [models.Index(fields=["store", "snapshot_date"], name="inventory_store_day")],
                "constraints": [models.UniqueConstraint(fields=("store", "variant", "snapshot_date"), name="uniq_inventory_snapshot_day")],
            },
        ),
        migrations.CreateModel(
            name="StorefrontEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_id", models.UUIDField(default=uuid.uuid4)),
                ("event_type", models.CharField(choices=[("page_view", "Page view"), ("product_view", "Product view"), ("search", "Search"), ("add_to_cart", "Add to cart"), ("checkout_started", "Checkout started"), ("purchase", "Purchase")], max_length=32)),
                ("visitor_hash", models.CharField(blank=True, default="", max_length=64)),
                ("properties", models.JSONField(blank=True, default=dict)),
                ("occurred_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("order", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analytics_events", to="orders.order")),
                ("product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analytics_events", to="catalog.product")),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="analytics_events", to="store.store")),
                ("variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analytics_events", to="catalog.stockvariant")),
            ],
            options={
                "ordering": ["-occurred_at", "-id"],
                "indexes": [models.Index(fields=["store", "event_type", "occurred_at"], name="analytics_store_type_time"), models.Index(fields=["store", "occurred_at"], name="analytics_store_time")],
                "constraints": [models.UniqueConstraint(fields=("store", "event_id"), name="uniq_analytics_event_per_store")],
            },
        ),
    ]
