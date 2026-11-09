from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("platform_admin", "0004_platformpresentation"),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformMonitoringSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton_key", models.PositiveSmallIntegerField(default=1, editable=False, unique=True)),
                ("enabled", models.BooleanField(default=True)),
                ("alert_email", models.EmailField(default="tijarago.services@gmail.com", max_length=254)),
                ("reminder_minutes", models.PositiveIntegerField(default=60)),
                ("last_alert_signature", models.CharField(blank=True, default="", max_length=64)),
                ("last_alert_sent_at", models.DateTimeField(blank=True, null=True)),
                ("last_test_sent_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="platform_monitoring_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "platform monitoring settings"},
        ),
    ]
