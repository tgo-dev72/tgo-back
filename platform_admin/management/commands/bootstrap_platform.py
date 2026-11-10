import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.designs import list_designs
from storefront_factory.jobs import execute_and_reconcile_local_deployment
from storefront_factory.models import StorefrontRequest
from storefront_factory.services import storefront_health_is_ready
from subscriptions.bootstrap import restore_commercial_reference_data
from theme_previewer.models import ThemeDemoStore
from theme_previewer.provisioning import provision_theme_demo


class Command(BaseCommand):
    help = (
        "Idempotently restore mandatory platform data and official theme demos. "
        "Use --deploy to publish and enable missing demo storefronts."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--deploy",
            action="store_true",
            help="Deploy, health-check, and enable official theme demo storefronts.",
        )
        parser.add_argument(
            "--theme",
            action="append",
            dest="themes",
            help="Theme key; repeat to limit provisioning. Default: every ready theme.",
        )

    def handle(self, *args, **options):
        commercial = restore_commercial_reference_data()
        self.stdout.write(
            "Commercial catalog ready: "
            f"{commercial['capabilities']} capabilities, {commercial['plans']} plans."
        )

        ready = {item["key"] for item in list_designs() if item.get("status") == "ready"}
        if not ready:
            raise CommandError(
                "No ready themes were found. Check DESIGN_STOCK_REPO, "
                "STOREFRONT_RELEASE_REVISION, and the source token."
            )
        themes = options["themes"] or sorted(ready)
        unknown = set(themes) - ready
        if unknown:
            raise CommandError(f"Unknown or unavailable themes: {', '.join(sorted(unknown))}")

        for theme in themes:
            try:
                profile = provision_theme_demo(theme)
                if options["deploy"]:
                    profile = self._deploy_and_enable(profile)
            except (RuntimeError, ValueError) as exc:
                raise CommandError(f"{theme}: {exc}") from exc
            state = "enabled" if profile.is_enabled else profile.deployment_status
            self.stdout.write(f"{theme}: {profile.store.slug} [{state}]")

        mode = "deployed and enabled" if options["deploy"] else "prepared"
        self.stdout.write(self.style.SUCCESS(
            f"Platform bootstrap complete: {len(themes)} theme demo(s) {mode}."
        ))

    @staticmethod
    def _deploy_and_enable(profile):
        deployment = StorefrontRequest.objects.get(pk=profile.deployment_id)
        public_url = deployment.deployed_url or deployment.domain
        healthy = (
            deployment.pipeline_state == StorefrontRequest.PIPELINE_LIVE
            and storefront_health_is_ready(
                public_url,
                expected_slug=deployment.slug,
                expected_revision=deployment.target_revision or None,
            )
        )

        if not healthy:
            if deployment.pipeline_state == StorefrontRequest.PIPELINE_LIVE:
                raise RuntimeError(
                    "the recorded live deployment failed its public health check; "
                    "release or repair it before bootstrapping again"
                )
            active_states = {
                StorefrontRequest.PIPELINE_PREPARING,
                StorefrontRequest.PIPELINE_PUBLISHING,
                StorefrontRequest.PIPELINE_PROVISIONING,
                StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
                StorefrontRequest.PIPELINE_BUILDING,
                StorefrontRequest.PIPELINE_VERIFYING,
            }
            if deployment.pipeline_state in active_states:
                raise RuntimeError("a deployment is already in progress")
            task_id = uuid.uuid4()
            deployment.task_id = task_id
            deployment.pipeline_state = StorefrontRequest.PIPELINE_ACCEPTED
            deployment.save(update_fields=["task_id", "pipeline_state"])
            result = execute_and_reconcile_local_deployment(
                deployment.pk,
                task_id=task_id,
            )
            deployment.refresh_from_db()
            if not result.get("terminal") or deployment.pipeline_state != StorefrontRequest.PIPELINE_LIVE:
                raise RuntimeError(
                    deployment.internal_error or "the storefront deployment did not become live"
                )
            public_url = deployment.deployed_url or deployment.domain
            if not storefront_health_is_ready(
                public_url,
                expected_slug=deployment.slug,
                expected_revision=deployment.target_revision or None,
            ):
                raise RuntimeError("the deployed storefront failed its public health check")

        with transaction.atomic():
            profile = ThemeDemoStore.objects.select_for_update().get(pk=profile.pk)
            profile.public_url = public_url
            profile.deployment_status = ThemeDemoStore.DeploymentStatus.READY
            profile.is_enabled = True
            profile.last_validated_at = timezone.now()
            profile.full_clean()
            profile.save(update_fields=[
                "public_url", "deployment_status", "is_enabled",
                "last_validated_at", "updated_at",
            ])
        return profile
