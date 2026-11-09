from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("store", "0023_storeconfig_default_locale"),
        ("storefront_factory", "0011_storefrontrepositorymigration"),
    ]
    operations = [
        migrations.CreateModel(
            name="ThemeDemoStore",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("theme_key", models.SlugField(max_length=50, unique=True)),
                ("public_url", models.URLField(blank=True, default="", max_length=300)),
                ("is_enabled", models.BooleanField(default=False)),
                ("deployment_status", models.CharField(choices=[("draft", "Draft"), ("prepared", "Prepared"), ("ready", "Ready"), ("failed", "Failed")], default="draft", max_length=16)),
                ("catalog_revision", models.CharField(blank=True, default="", max_length=64)),
                ("last_validated_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="theme_demo_profile", to="store.store")),
                ("deployment", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="theme_demo_profile", to="storefront_factory.storefrontrequest")),
            ],
            options={"ordering": ("theme_key",)},
        ),
    ]
