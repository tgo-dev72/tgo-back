from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("payments", "0004_platformpaymentgateway")]

    operations = [
        migrations.AddField(
            model_name="platformpaymentgateway",
            name="sandbox_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
