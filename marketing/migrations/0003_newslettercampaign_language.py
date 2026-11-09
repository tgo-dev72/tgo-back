from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("marketing", "0002_campaign_delivery")]

    operations = [
        migrations.AddField(
            model_name="newslettercampaign",
            name="language",
            field=models.CharField(
                choices=[("en", "English"), ("fr", "French"), ("ar", "Arabic")],
                default="en",
                max_length=2,
            ),
        ),
    ]
