import decimal

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PremiumPlanSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("price", models.DecimalField(decimal_places=2, default=decimal.Decimal("99.00"), help_text="Premium subscription price in the configured currency.", max_digits=10, validators=[django.core.validators.MinValueValidator(decimal.Decimal("5.00"))])),
                ("currency", models.CharField(default="MAD", max_length=3, validators=[django.core.validators.RegexValidator("^[A-Z]{3}$", "Use a three-letter ISO currency code.")])),
                ("duration_days", models.PositiveIntegerField(default=30, help_text="Number of entitlement days granted by one successful payment.", validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(3650)])),
                ("entitlement_scope", models.CharField(choices=[("all_owned_stores", "All stores owned by the subscriber"), ("one_store", "One selected store")], default="all_owned_stores", max_length=24)),
                ("is_active", models.BooleanField(default=False, help_text="When disabled, merchants cannot initiate a Premium checkout.")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="premium_pricing_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Premium plan settings",
                "verbose_name_plural": "Premium plan settings",
            },
        ),
    ]
