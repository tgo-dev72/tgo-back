from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import migrations, models
from django.db.models import F, Q


def backfill_order_allocations(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    OrderedProduct = apps.get_model("orders", "OrderedProduct")
    QuantityException = apps.get_model("orders", "QuantityException")

    exception_deltas = dict(QuantityException.objects.values_list("id", "delta_quantity"))
    for line in OrderedProduct.objects.select_related("order").iterator():
        shortfall = exception_deltas.get(line.exception_id, 0) if line.exception_id else 0
        fulfilled = max(0, line.quantity - shortfall)
        reservation_held = (
            line.order.status == "pending"
            and line.order.is_paid != "failed"
            and not line.stock_deducted
        )
        line.fulfilled_quantity = fulfilled
        line.reserved_quantity = fulfilled if reservation_held else 0
        line.save(update_fields=["fulfilled_quantity", "reserved_quantity"])

    # Historical shipping configuration is not immutable, so it cannot be
    # reconstructed safely. Preserve the charged total as the legacy subtotal;
    # all new orders snapshot the exact split.
    Order.objects.update(subtotal=F("amount"), shipping_amount=0)


class Migration(migrations.Migration):
    dependencies = [("orders", "0007_order_marketing_consent_and_acknowledgement")]

    operations = [
        migrations.AddField(
            model_name="order",
            name="loyalty_points_awarded",
            field=models.PositiveIntegerField(default=0, help_text="Exact points granted by this order, used for safe reversal."),
        ),
        migrations.AddField(
            model_name="order",
            name="shipping_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                validators=[MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="subtotal",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=12,
                validators=[MinValueValidator(Decimal("0.00"))],
            ),
        ),
        migrations.AddField(
            model_name="orderedproduct",
            name="fulfilled_quantity",
            field=models.PositiveIntegerField(default=0, help_text="Quantity allocated to this order when it was placed."),
        ),
        migrations.AddField(
            model_name="orderedproduct",
            name="reserved_quantity",
            field=models.PositiveIntegerField(default=0, help_text="Current variant reservation owned by this order line."),
        ),
        migrations.RunPython(backfill_order_allocations, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="orderedproduct",
            constraint=models.CheckConstraint(condition=Q(fulfilled_quantity__lte=F("quantity")), name="order_line_fulfilled_not_above_requested"),
        ),
        migrations.AddConstraint(
            model_name="orderedproduct",
            constraint=models.CheckConstraint(condition=Q(reserved_quantity__lte=F("fulfilled_quantity")), name="order_line_reserved_not_above_fulfilled"),
        ),
    ]
