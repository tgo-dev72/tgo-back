from django.core.management.base import BaseCommand

from subscriptions.bootstrap import restore_commercial_reference_data


class Command(BaseCommand):
    help = "Restore mandatory plans and capability definitions without deleting merchant data."

    def handle(self, *args, **options):
        result = restore_commercial_reference_data()
        self.stdout.write(self.style.SUCCESS(
            "Commercial catalog ready: "
            f"{result['capabilities']} capabilities, {result['plans']} plans "
            f"({result['capabilities_created']} capabilities and "
            f"{int(result['free_plan_created'])} baseline plan restored)."
        ))
