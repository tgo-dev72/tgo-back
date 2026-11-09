from datetime import date

from django.core.management.base import BaseCommand, CommandError

from commerce_analytics.services import capture_inventory_snapshot
from store.models import Store


class Command(BaseCommand):
    help = "Capture an idempotent daily inventory snapshot for every store or one store."

    def add_arguments(self, parser):
        parser.add_argument("--store", dest="store_slug", help="Capture only this store slug.")
        parser.add_argument("--date", dest="snapshot_date", help="Snapshot date in YYYY-MM-DD format.")

    def handle(self, *args, **options):
        snapshot_date = None
        if options["snapshot_date"]:
            try:
                snapshot_date = date.fromisoformat(options["snapshot_date"])
            except ValueError as exc:
                raise CommandError("--date must use YYYY-MM-DD.") from exc
        stores = Store.objects.all()
        if options["store_slug"]:
            stores = stores.filter(slug=options["store_slug"])
            if not stores.exists():
                raise CommandError("Store not found.")
        total = sum(capture_inventory_snapshot(store, snapshot_date) for store in stores.iterator())
        self.stdout.write(self.style.SUCCESS(f"Captured {total} inventory variant snapshots."))
