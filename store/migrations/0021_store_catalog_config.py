from copy import deepcopy

from django.db import migrations, models
import django.db.models.deletion


def preserve_catalog_defaults(apps, schema_editor):
    Store = apps.get_model('store', 'Store')
    RetailType = apps.get_model('catalog', 'RetailType')
    alias = schema_editor.connection.alias
    general = RetailType.objects.using(alias).filter(key='general').first()
    for store in Store.objects.using(alias).select_related('retail_type').iterator():
        preset = store.retail_type or general
        Store.objects.using(alias).filter(pk=store.pk).update(
            catalog_config=deepcopy(preset.config) if preset else {},
        )


class Migration(migrations.Migration):
    dependencies = [('store', '0020_alter_storeconfig_design'), ('catalog', '0013_product_merchandising_choices')]
    operations = [
        migrations.AddField(model_name='store', name='catalog_config', field=models.JSONField(blank=True, default=dict)),
        migrations.AlterField(model_name='store', name='retail_type', field=models.ForeignKey(blank=True, help_text='Informational preset used for initial catalog defaults.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='stores', to='catalog.retailtype')),
        migrations.RunPython(preserve_catalog_defaults, migrations.RunPython.noop),
    ]
