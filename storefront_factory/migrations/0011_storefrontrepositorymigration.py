from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("storefront_factory", "0010_repository_coordinates")]

    operations = [
        migrations.CreateModel(
            name="StorefrontRepositoryMigration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("state", models.CharField(default="prepared", max_length=32)),
                ("source", models.JSONField(default=dict)),
                ("destination", models.JSONField(default=dict)),
                ("candidate_deployment_id", models.CharField(blank=True, max_length=100)),
                ("candidate_url", models.URLField(blank=True, max_length=300)),
                ("action_token", models.UUIDField(blank=True, editable=False, null=True)),
                ("action_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("journal", models.JSONField(default=list)),
                ("rollback_verified_at", models.DateTimeField(blank=True, null=True)),
                ("retain_until", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("request", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="repository_migration", to="storefront_factory.storefrontrequest")),
            ],
        ),
    ]
