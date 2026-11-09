from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_passwordresettoken")]

    operations = [
        migrations.AlterField(
            model_name="merchantprofile",
            name="preferred_language",
            field=models.CharField(
                choices=[("en", "English"), ("fr", "French"), ("ar", "Arabic")],
                default="ar",
                max_length=2,
            ),
        ),
    ]
