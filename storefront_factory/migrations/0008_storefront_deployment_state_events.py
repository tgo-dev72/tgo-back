import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def backfill_deployment_truth(apps, schema_editor):
    StorefrontRequest = apps.get_model("storefront_factory", "StorefrontRequest")
    StorefrontDeploymentEvent = apps.get_model("storefront_factory", "StorefrontDeploymentEvent")
    now = timezone.now()

    for request in StorefrontRequest.objects.all().iterator():
        if request.deploy_status == "deployed":
            state, progress, message = "live", 100, "deployment.live"
            request.status = "created"
            request.completed_at = now
        elif request.status == "failed" or request.deploy_status == "failed":
            state, progress, message = "failed", 0, "deployment.failed"
            request.status = "failed"
            request.deploy_status = "failed"
            request.completed_at = now
        elif request.deploy_status == "deploying" and request.vercel_deployment_id:
            state, progress, message = "building", 82, "deployment.building"
            request.status = "scaffolding"
        elif request.status == "scaffolding" or request.deploy_status == "deploying":
            # A pre-migration in-process worker cannot be proven alive. Require
            # an explicit retry instead of silently duplicating provider work.
            state, progress, message = "failed", 0, "deployment.interrupted"
            request.status = "failed"
            request.deploy_status = "failed"
            request.completed_at = now
            request.internal_error = "Legacy deployment was interrupted before durable task tracking was introduced."
        else:
            state, progress, message = "failed", 0, "deployment.not_started"
            request.status = "failed"
            request.deploy_status = "failed"
            request.completed_at = now

        request.pipeline_state = state
        request.progress = progress
        request.public_message = message
        request.event_version = 1
        request.last_heartbeat_at = now
        request.save(update_fields=[
            "pipeline_state", "progress", "public_message", "event_version",
            "last_heartbeat_at", "completed_at", "internal_error", "status",
            "deploy_status",
        ])
        StorefrontDeploymentEvent.objects.create(
            storefront=request,
            version=1,
            state=state,
            progress=progress,
            message=message,
            snapshot={
                "id": request.pk,
                "store": request.store_id,
                "design": request.design,
                "name": request.name,
                "slug": request.slug,
                "status": request.status,
                "deploy_status": request.deploy_status,
                "pipeline_state": state,
                "progress": progress,
                "public_message": message,
                "event_version": 1,
                "deployed_url": request.deployed_url,
                "app_path": request.app_path,
                "port": request.port,
                "log": "",
                "deploy_log": "",
                "created_at": request.created_at.isoformat() if request.created_at else "",
            },
        )


def reverse_backfill(apps, schema_editor):
    StorefrontDeploymentEvent = apps.get_model("storefront_factory", "StorefrontDeploymentEvent")
    StorefrontDeploymentEvent.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("storefront_factory", "0007_storefrontrequest_vercel_deployment_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="storefrontrequest",
            name="attempt_count",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="cloudflare_dns_record_id",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="github_commit_sha",
            field=models.CharField(blank=True, editable=False, max_length=64),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="completed_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="event_version",
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="internal_error",
            field=models.TextField(blank=True, default="", editable=False),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="last_heartbeat_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="pipeline_state",
            field=models.CharField(
                choices=[
                    ("accepted", "Accepted"),
                    ("preparing", "Preparing storefront"),
                    ("publishing", "Publishing storefront"),
                    ("provisioning", "Provisioning hosting"),
                    ("configuring_domain", "Configuring domain"),
                    ("building", "Building storefront"),
                    ("verifying", "Verifying public storefront"),
                    ("live", "Live"),
                    ("failed", "Failed"),
                ],
                db_index=True,
                default="accepted",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="public_message",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="started_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="storefrontrequest",
            name="task_id",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.CreateModel(
            name="StorefrontDeploymentEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveBigIntegerField()),
                ("state", models.CharField(choices=[("accepted", "Accepted"), ("preparing", "Preparing storefront"), ("publishing", "Publishing storefront"), ("provisioning", "Provisioning hosting"), ("configuring_domain", "Configuring domain"), ("building", "Building storefront"), ("verifying", "Verifying public storefront"), ("live", "Live"), ("failed", "Failed")], max_length=32)),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("message", models.CharField(blank=True, default="", max_length=200)),
                ("snapshot", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("storefront", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deployment_events", to="storefront_factory.storefrontrequest")),
            ],
            options={"ordering": ["version"]},
        ),
        migrations.AddConstraint(
            model_name="storefrontdeploymentevent",
            constraint=models.UniqueConstraint(fields=("storefront", "version"), name="uniq_storefront_deployment_event_version"),
        ),
        migrations.RunPython(backfill_deployment_truth, reverse_backfill),
    ]
