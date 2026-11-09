import json
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from storefront_factory import repository_migration as workflow
from storefront_factory.models import StorefrontRepositoryMigration, StorefrontRequest
from storefront_factory.vercel_deploy import DeployError


class Command(BaseCommand):
    help = "Prepare/build/promote/restore/finalize one recorded storefront repository migration."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("prepare", "build", "promote", "restore", "finalize", "cancel", "status", "unlock"))
        parser.add_argument("request_id", type=int)
        parser.add_argument("--confirm-slug", default="")
        parser.add_argument("--approval", default="", help="Change ticket or explicit operator approval reference; never a secret.")
        parser.add_argument("--evidence", default="", help="Preview/business-data verification report reference.")
        parser.add_argument("--retention-hours", type=int, default=24)
        parser.add_argument("--stopped-action-token", default="", help="Unlock only after confirming that this exact worker has stopped.")

    def handle(self, *args, **options):
        action = options["action"]
        request_id = options["request_id"]
        kwargs = {"confirm_slug": options["confirm_slug"]}
        try:
            if action == "status":
                migration = StorefrontRepositoryMigration.objects.get(request_id=request_id)
            elif action == "unlock":
                if not options["approval"].strip() or not options["stopped_action_token"]:
                    raise DeployError("Confirm the worker stopped and record an approval before unlocking.")
                with transaction.atomic():
                    req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
                    workflow._confirmed(req, options["confirm_slug"])
                    migration = StorefrontRepositoryMigration.objects.select_for_update().get(request=req)
                    if migration.action_token != uuid.UUID(options["stopped_action_token"]):
                        raise DeployError("The action identity changed; refusing to unlock another worker.")
                    migration.action_token = None
                    workflow._checkpoint(migration, journal=[*migration.journal, {
                        "action": "worker_confirmed_stopped", "approval": options["approval"],
                    }])
            else:
                if action == "prepare":
                    kwargs.update(approval=options["approval"], retention_hours=options["retention_hours"])
                elif action == "promote":
                    kwargs.update(evidence=options["evidence"])
                elif action == "finalize":
                    kwargs.update(approval=options["approval"])
                migration = getattr(workflow, action)(request_id, **kwargs)
        except (DeployError, ValueError, StorefrontRequest.DoesNotExist, StorefrontRepositoryMigration.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({
            "migration_id": migration.pk, "request_id": migration.request_id,
            "state": migration.state, "candidate_url": migration.candidate_url,
            "candidate_deployment_id": migration.candidate_deployment_id,
            "action_token": str(migration.action_token or ""), "last_error": migration.last_error,
            "rollback_verified_at": str(migration.rollback_verified_at or ""),
            "retain_until": str(migration.retain_until or ""),
            "source": migration.source, "destination": migration.destination,
        }, indent=2))
