import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketing", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="newslettercampaign",
            name="delivery_task_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.CreateModel(
            name="CampaignDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("sent", "Sent"), ("failed", "Failed")], default="pending", max_length=12)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("error", models.CharField(blank=True, default="", max_length=500)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="marketing.newslettercampaign")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddConstraint(
            model_name="campaigndelivery",
            constraint=models.UniqueConstraint(fields=("campaign", "email"), name="unique_campaign_recipient"),
        ),
        migrations.AddIndex(
            model_name="campaigndelivery",
            index=models.Index(fields=["campaign", "status"], name="campaign_delivery_status_idx"),
        ),
    ]
