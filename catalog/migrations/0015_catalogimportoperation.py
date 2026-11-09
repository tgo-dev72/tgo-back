import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("catalog", "0014_seed_lux_showcase_catalog"),
    ]
    operations = [
        migrations.CreateModel(
            name="CatalogImportOperation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("idempotency_key", models.CharField(max_length=128)),
                ("payload_hash", models.CharField(max_length=64)),
                ("rows", models.JSONField(default=list)),
                ("validation_errors", models.JSONField(default=list)),
                ("result", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("validated", "Validated"), ("committed", "Committed"), ("failed", "Failed")], default="validated", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="catalog_imports", to="store.store")),
                ("submitted_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("store", "idempotency_key"), name="uniq_catalog_import_idempotency_per_store")]},
        ),
    ]
