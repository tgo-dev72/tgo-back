from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0013_paid_plans_usd")]

    operations = [
        migrations.AddField(
            model_name="subscriptionpayment",
            name="sandbox",
            field=models.BooleanField(default=True),
        ),
    ]
