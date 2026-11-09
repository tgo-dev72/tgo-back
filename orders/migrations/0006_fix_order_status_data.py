"""Fix order status data that was boolean → string during the field type change.

Old boolean True → 'confirmed' (if is_paid is confirmed/cod) or 'pending'
Old boolean False → 'pending' (default for new orders)
"""
from django.db import migrations


def forward(apps, schema_editor):
    Order = apps.get_model("orders", "Order")

    # status=True but is_paid still pending → should be confirmed
    Order.objects.filter(status="True", is_paid="pending").update(status="confirmed")

    # status=True and is_paid confirmed/cod → confirmed
    Order.objects.filter(status="True", is_paid__in=("confirmed", "cod")).update(status="confirmed")

    # status=True and is_paid failed → failed orders that were once confirmed
    Order.objects.filter(status="True", is_paid="failed").update(status="cancelled")

    # status=False → pending
    Order.objects.filter(status="False").update(status="pending")

    # Any remaining True/False strings → pending
    Order.objects.filter(status="True").update(status="pending")
    Order.objects.filter(status="False").update(status="pending")


def backward(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    Order.objects.filter(status__in=("confirmed", "shipped", "delivered")).update(status="True")
    Order.objects.filter(status__in=("pending", "cancelled")).update(status="False")


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0005_remove_order_delivered_order_cancel_reason_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
