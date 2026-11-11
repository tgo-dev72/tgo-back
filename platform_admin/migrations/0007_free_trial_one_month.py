from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [("platform_admin", "0006_platformcommercesettings")]
    operations = [
        migrations.AlterField(
            model_name="platformcommercesettings",
            name="free_trial_days",
            field=models.PositiveIntegerField(default=30, help_text="Number of days a newly granted Free trial remains usable.", validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(365)]),
        ),
    ]
