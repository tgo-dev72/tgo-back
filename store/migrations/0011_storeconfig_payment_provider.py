from django.db import migrations, models


def move_credentials_into_provider_vault(apps, schema_editor):
    StoreConfig = apps.get_model("store", "StoreConfig")
    for config in StoreConfig.objects.exclude(payment_keys={}).iterator():
        values = config.payment_keys
        if isinstance(values, dict) and not any(isinstance(value, dict) for value in values.values()):
            config.payment_keys = {config.payment_provider: values}
            config.save(update_fields=["payment_keys"])


def restore_flat_credentials(apps, schema_editor):
    StoreConfig = apps.get_model("store", "StoreConfig")
    for config in StoreConfig.objects.iterator():
        values = config.payment_keys
        if isinstance(values, dict) and isinstance(values.get(config.payment_provider), dict):
            config.payment_keys = values[config.payment_provider]
            config.save(update_fields=["payment_keys"])


class Migration(migrations.Migration):
    dependencies = [("store", "0010_storecustomer_storemember_uniq_owner_per_store_and_more")]

    operations = [
        migrations.AddField(
            model_name="storeconfig",
            name="payment_provider",
            field=models.CharField(
                default="youcanpay",
                help_text="Payment gateway adapter code. Existing attempts retain their original provider.",
                max_length=40,
            ),
        ),
        migrations.AlterField(
            model_name="storeconfig",
            name="payment_keys",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Per-provider credential vault interpreted only by payment adapters.",
            ),
        ),
        migrations.RunPython(move_credentials_into_provider_vault, restore_flat_credentials),
    ]
