from datetime import timedelta

from django.conf import settings
from django.db import migrations, models
import django.core.validators
from django.utils import timezone


def expire_legacy_free_trials(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    free_plan = SubscriptionPlan.objects.filter(slug="free").first()
    if free_plan is None:
        return
    UserSubscription.objects.filter(
        plan_id=free_plan.pk,
        status="active",
        current_period_end__isnull=True,
    ).update(current_period_end=timezone.now() + timedelta(days=14))


class Migration(migrations.Migration):
    dependencies = [
        ("platform_admin", "0005_platformmonitoringsettings"),
        ("subscriptions", "0015_invoicing_entitlement"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformCommerceSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton_key", models.PositiveSmallIntegerField(default=1, editable=False, unique=True)),
                ("free_trial_days", models.PositiveIntegerField(default=14, help_text="Number of days a newly granted Free trial remains usable.", validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(365)])),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="platform_commerce_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "platform commerce settings"},
        ),
        migrations.RunPython(expire_legacy_free_trials, migrations.RunPython.noop),
    ]
