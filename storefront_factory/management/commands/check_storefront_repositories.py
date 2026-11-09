"""Read-only provider preflight; never prints credentials or creates resources."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from storefront_factory.cloud import _git_api, preflight_destination, validate_pipeline_configuration
from storefront_factory.vercel_deploy import DeployError


class Command(BaseCommand):
    help = "Check configured storefront source revision and destination (read-only; no deployment)."

    def handle(self, *args, **options):
        try:
            validate_pipeline_configuration()
            source = settings.DESIGN_STOCK_REPO
            target = settings.STOREFRONT_DEPLOYMENT_REPO or source
            branch = settings.STOREFRONT_DEPLOYMENT_BRANCH if target != source else "main"
            revision = settings.STOREFRONT_RELEASE_REVISION
            if not revision:
                raise DeployError("Pin STOREFRONT_RELEASE_REVISION before release checks.")
            _git_api(f"git/commits/{revision}", repository=source)
            preflight_destination(target, branch, isolated=target != source)
        except (DeployError, KeyError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Source readable: {source}@{revision}"))
        self.stdout.write(self.style.SUCCESS(f"Destination readable: {target}:{branch}"))
        self.stdout.write("Read-only checks passed. Write permission and Vercel GitHub access still require a disposable canary.")
