import uuid

from django.core.management.base import BaseCommand

from store.domain_jobs import execute_domain_disconnection, execute_domain_verification


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("domain_id", type=int)
        parser.add_argument("operation_id", type=uuid.UUID)
        parser.add_argument("--action", choices=("verify", "remove"), default="verify")

    def handle(self, *args, **options):
        executor = execute_domain_disconnection if options["action"] == "remove" else execute_domain_verification
        executor(options["domain_id"], options["operation_id"])
