from decimal import Decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("catalog", "0011_restore_retail_type_reference_data"),
        ("store", "0018_clientprofile_phone_verified_at_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="Coupon",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=32, validators=[django.core.validators.RegexValidator("^[A-Za-z0-9][A-Za-z0-9_-]*$", "Use letters, numbers, hyphens, or underscores.")])),
                ("discount_type", models.CharField(choices=[("percentage", "Percentage"), ("fixed", "Fixed amount")], max_length=16)),
                ("value", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))])),
                ("scope", models.CharField(choices=[("all_products", "All products"), ("categories", "Selected categories"), ("products", "Selected products")], default="all_products", max_length=16)),
                ("minimum_subtotal", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))])),
                ("maximum_discount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))])),
                ("total_usage_limit", models.PositiveIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(1)])),
                ("per_customer_usage_limit", models.PositiveIntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(1)])),
                ("usage_count", models.PositiveIntegerField(default=0, editable=False)),
                ("starts_at", models.DateTimeField(blank=True, null=True)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("categories", models.ManyToManyField(blank=True, related_name="coupons", to="catalog.catalogsection")),
                ("products", models.ManyToManyField(blank=True, related_name="coupons", to="catalog.product")),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="coupons", to="store.store")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(model_name="coupon", constraint=models.UniqueConstraint(fields=("store", "code"), name="uniq_coupon_code_per_store")),
        migrations.AddConstraint(model_name="coupon", constraint=models.CheckConstraint(condition=models.Q(("value__gt", 0)), name="coupon_value_positive")),
        migrations.AddConstraint(model_name="coupon", constraint=models.CheckConstraint(condition=models.Q(("minimum_subtotal__gte", 0)), name="coupon_minimum_nonnegative")),
        migrations.AddConstraint(model_name="coupon", constraint=models.CheckConstraint(condition=models.Q(("discount_type", "fixed"), ("value__lte", 100), _connector="OR"), name="coupon_percentage_at_most_100")),
    ]
