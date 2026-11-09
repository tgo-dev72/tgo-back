"""Restore mandatory, non-merchant platform reference data."""

from django.core.management.base import BaseCommand

from catalog.models import RetailType
from catalog.seeds import ensure_retail_types


class Command(BaseCommand):
    help = "Restore mandatory platform reference data without deleting merchant data."

    def handle(self, *args, **options):
        before = RetailType.objects.count()
        expected = ensure_retail_types()
        after = RetailType.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Retail types ready: {after}/{expected} "
                f"({max(after - before, 0)} restored)."
            )
        )
