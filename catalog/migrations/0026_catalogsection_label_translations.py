from django.db import migrations, models


def backfill_english_names(apps, schema_editor):
    Section = apps.get_model("catalog", "CatalogSection")
    for section in Section.objects.all().only("id", "label").iterator():
        Section.objects.filter(pk=section.pk).update(
            label_translations={"en": section.label, "fr": "", "ar": ""},
        )


class Migration(migrations.Migration):
    dependencies = [("catalog", "0025_remove_product_tax_rate")]
    operations = [
        migrations.AddField(
            model_name="catalogsection",
            name="label_translations",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(backfill_english_names, migrations.RunPython.noop),
    ]
