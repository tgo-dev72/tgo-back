import django.core.validators
from django.db import migrations, models
from django.utils import timezone


def mark_existing_accounts_verified(apps, schema_editor):
    AuthUser = apps.get_model("accounts", "AuthUser")
    AuthUser.objects.filter(is_active=True, email_verified_at__isnull=True).update(
        email_verified_at=timezone.now()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_merchantprofile"),
    ]

    operations = [
        migrations.AddField(
            model_name="authuser",
            name="email_verification_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="authuser",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="business_name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="phone_number",
            field=models.CharField(
                blank=True,
                default="",
                max_length=16,
                validators=[
                    django.core.validators.RegexValidator(
                        "^\\+[1-9]\\d{7,14}$",
                        "Use an international phone number such as +212612345678.",
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="phone_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="preferred_language",
            field=models.CharField(
                choices=[("en", "English"), ("fr", "French"), ("ar", "Arabic")],
                default="en",
                max_length=2,
            ),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="terms_accepted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="merchantprofile",
            name="terms_version",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.RunPython(mark_existing_accounts_verified, migrations.RunPython.noop),
    ]
