"""Read-only prerequisite check; does not claim a hosted rollback was rehearsed."""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from storefront_factory.models import StorefrontRequest
from storefront_factory.repositories import destination, github_token, validate_coordinates
from storefront_factory.repository_guards import migration_in_progress


class Command(BaseCommand):
    help = "Check expanded schema, retained destination access and quiescence before a flag/backend rollback."

    def handle(self, *args, **options):
        executor = MigrationExecutor(connection)
        required = ("storefront_factory", "0011_storefrontrepositorymigration")
        if executor.migration_plan([required]):
            raise CommandError("Keep the expanded schema: migration 0011 must be applied before rollback checks.")
        if settings.STOREFRONT_NEW_REPO_ENABLED:
            raise CommandError("Set STOREFRONT_NEW_REPO_ENABLED=false on the disposable rollback target first.")
        count = 0
        for req in StorefrontRequest.objects.all().iterator():
            try:
                validate_coordinates(*destination(req), req.slug)
                if not github_token(destination(req)[0], write=True):
                    raise ValueError("Retain the write credential for every recorded destination.")
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            if migration_in_progress(req.pk):
                raise CommandError("Finish or restore/cancel active repository migrations before a backend rollback.")
            if req.pipeline_state not in (req.PIPELINE_LIVE, req.PIPELINE_FAILED):
                raise CommandError("Drain active deployment jobs before a backend rollback.")
            count += 1
        self.stdout.write(json.dumps({"expanded_schema_present": True,
            "new_repository_default": False, "recorded_destinations_access_configured": True,
            "stores_checked": count, "scope": "Read-only prerequisites; hosted rollback still needs execution evidence."}))
