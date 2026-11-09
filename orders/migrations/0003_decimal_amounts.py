# Generated manually to preserve existing order snapshots while switching to fixed precision.
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0002_alter_quantityexception_id"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="amount",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))]),
        ),
        migrations.AlterField(
            model_name="orderedproduct",
            name="price",
            field=models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))]),
        ),
    ]
