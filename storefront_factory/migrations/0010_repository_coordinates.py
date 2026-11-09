from django.conf import settings
from django.db import migrations, models


def pin_existing_requests(apps, schema_editor):
    Request = apps.get_model("storefront_factory", "StorefrontRequest")
    # Run before changing DESIGN_STOCK_REPO. A new deployment destination does
    # not redirect historical requests, even ones awaiting their first retry.
    for request in Request.objects.using(schema_editor.connection.alias).all().iterator():
        request.source_repository = settings.DESIGN_STOCK_REPO
        request.deployment_repository = settings.DESIGN_STOCK_REPO
        request.deployment_branch = "main"
        request.deployment_app_path = f"apps/{request.slug}"
        request.save(update_fields=["source_repository", "deployment_repository", "deployment_branch", "deployment_app_path"])


class Migration(migrations.Migration):
    dependencies = [("storefront_factory", "0009_storefront_release_tracking")]
    operations = [
        migrations.AddField(model_name="storefrontrequest", name=name,
                            field=models.CharField(max_length=length, blank=True, editable=False))
        for name, length in [("source_repository", 200), ("deployment_repository", 200),
                             ("deployment_branch", 100), ("deployment_app_path", 200)]
    ] + [
        migrations.AddField(model_name="storefrontrequest", name="deployment_layout",
                            field=models.CharField(max_length=16, default="legacy", editable=False,
                            choices=[("legacy", "Legacy workspace"), ("isolated", "Isolated bundle")])),
        migrations.RunPython(pin_existing_requests, migrations.RunPython.noop),
    ]
