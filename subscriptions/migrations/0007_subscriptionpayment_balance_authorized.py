from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0006_subscriptionpayment_credit_reserved_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionpayment",
            name="balance_authorized",
            field=models.BooleanField(
                default=False,
                help_text="The merchant explicitly authorized using account credit for this checkout.",
            ),
        ),
    ]
