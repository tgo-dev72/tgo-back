from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import platform_admin.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("platform_admin", "0003_useradministrationevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformPresentation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton_key", models.PositiveSmallIntegerField(default=1, editable=False, unique=True)),
                ("contact_email", models.EmailField(blank=True, default="", max_length=254)),
                ("support_phone", models.CharField(blank=True, default="", max_length=32)),
                ("whatsapp_phone", models.CharField(blank=True, default="", max_length=32)),
                ("social_links", models.JSONField(blank=True, default=dict, validators=[platform_admin.models.validate_platform_social_links])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="platform_presentation_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "platform showroom profile"},
        ),
    ]
