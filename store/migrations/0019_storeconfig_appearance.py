from django.db import migrations, models

import store.models


class Migration(migrations.Migration):
    dependencies = [("store", "0018_clientprofile_phone_verified_at_and_more")]

    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="appearance",
            field=models.JSONField(
                default=store.models.default_storefront_appearance,
                help_text="Versioned provider-neutral typography, header, surface, and footer settings.",
                validators=[store.models.validate_storefront_appearance],
            ),
        ),
    ]
