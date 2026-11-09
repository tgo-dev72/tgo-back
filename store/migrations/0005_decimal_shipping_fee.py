# Generated manually to preserve existing shipping values while switching to fixed precision.
from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("store", "0004_storeconfig_contact_email_storeconfig_design_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="storeconfig",
            name="shipping_fee",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), help_text="Flat shipping fee.", max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))]),
        ),
    ]
