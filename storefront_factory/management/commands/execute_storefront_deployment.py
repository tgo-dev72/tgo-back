from django.core.management.base import BaseCommand, CommandError

from storefront_factory.jobs import (
    DeploymentAlreadyRunning,
    execute_and_reconcile_local_deployment,
)


class Command(BaseCommand):
    help = "Execute one database-identified storefront deployment job."

    def add_arguments(self, parser):
        parser.add_argument("request_id", type=int)
        parser.add_argument("task_id")

    def handle(self, *args, **options):
        try:
            result = execute_and_reconcile_local_deployment(
                options["request_id"],
                task_id=options["task_id"],
            )
        except DeploymentAlreadyRunning as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(str(result))
