from django.core.management.base import BaseCommand
from django.utils import timezone

from store.domain_jobs import enqueue_domain_verification
from store.models import StorefrontDomain


class Command(BaseCommand):
    help = "Resumably queue due custom-domain verification checks without changing merchant DNS."

    def add_arguments(self, parser):
        parser.add_argument("--store", type=int)
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        queryset = StorefrontDomain.objects.filter(
            kind=StorefrontDomain.Kind.CUSTOM,
            status__in=[StorefrontDomain.Status.AWAITING_OWNERSHIP,
                        StorefrontDomain.Status.AWAITING_DNS, StorefrontDomain.Status.FAILED],
        ).filter(next_retry_at__isnull=True) | StorefrontDomain.objects.filter(
            kind=StorefrontDomain.Kind.CUSTOM, next_retry_at__lte=timezone.now(),
        )
        if options.get("store"):
            queryset = queryset.filter(store_id=options["store"])
        queued = 0
        for domain in queryset.order_by("last_checked_at", "pk")[:max(1, min(options["limit"], 500))]:
            if enqueue_domain_verification(domain.pk):
                queued += 1
        self.stdout.write(self.style.SUCCESS(f"Queued {queued} domain check(s)."))
