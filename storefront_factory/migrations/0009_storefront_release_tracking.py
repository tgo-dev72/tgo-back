from django.db import migrations, models


def backfill_release_tracking(apps, schema_editor):
    StorefrontRequest = apps.get_model("storefront_factory", "StorefrontRequest")
    for request in StorefrontRequest.objects.filter(
        pipeline_state="live",
        deploy_status="deployed",
    ).iterator():
        # Historical rows did not distinguish the source release from the
        # generated repository commit. The generated commit is nevertheless
        # an immutable, safe baseline until the next managed release.
        revision = request.github_commit_sha or ""
        request.target_revision = revision
        request.deployed_revision = revision
        request.last_known_good_revision = revision
        request.last_known_good_deployment_id = request.vercel_deployment_id or ""
        request.save(update_fields=[
            "target_revision",
            "deployed_revision",
            "last_known_good_revision",
            "last_known_good_deployment_id",
        ])


class Migration(migrations.Migration):
    dependencies = [("storefront_factory", "0008_storefront_deployment_state_events")]

    operations = [
        migrations.AddField(
            model_name="storefrontrequest",
            name="operation",
            field=models.CharField(
                choices=[
                    ("create", "Create storefront"),
                    ("update", "Update storefront release"),
                    ("redesign", "Change storefront design"),
                ],
                default="create",
                editable=False,
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="target_revision",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="deployed_revision",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="last_known_good_revision",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="last_known_good_deployment_id",
            field=models.CharField(blank=True, editable=False, max_length=100),
        ),
        migrations.RunPython(backfill_release_tracking, migrations.RunPython.noop),
    ]
