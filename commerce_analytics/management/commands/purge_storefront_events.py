from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from commerce_analytics.services import purge_expired_storefront_events


class Command(BaseCommand):
    help = "Delete expired behavioural events without touching commerce records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=settings.ANALYTICS_EVENT_RETENTION_DAYS,
            help="Retain this many days of behavioural events.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 30:
            raise CommandError("Event retention cannot be shorter than 30 days.")
        deleted = purge_expired_storefront_events(days)
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired analytics records."))
