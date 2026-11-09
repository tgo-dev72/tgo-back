from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("storefront_factory", "0006_alter_storefrontcategory_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="storefrontrequest",
            name="vercel_deployment_id",
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
    ]
