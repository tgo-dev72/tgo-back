from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0006_fix_order_status_data"),
        ("store", "0018_clientprofile_phone_verified_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="marketing_consent",
            field=models.BooleanField(
                default=False,
                help_text="Customer explicitly agreed to receive store campaigns at checkout.",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="items_acknowledged_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="items_acknowledged_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="acknowledged_orders",
                to="store.storemember",
            ),
        ),
    ]
