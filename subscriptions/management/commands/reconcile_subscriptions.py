from django.core.management.base import BaseCommand
from django.utils import timezone

from store.models import Store
from subscriptions.models import UserSubscription
from subscriptions.services import sync_store_entitlement


class Command(BaseCommand):
    help = "Reconcile cached store plan entitlements with active account subscriptions."

    def handle(self, *args, **options):
        now = timezone.now()
        expired_subscriptions = UserSubscription.objects.filter(
            status=UserSubscription.Status.ACTIVE,
            current_period_end__lte=now,
        ).update(status=UserSubscription.Status.EXPIRED, updated_at=now)
        premium = 0
        total = 0
        for store in Store.objects.iterator():
            total += 1
            premium += int(sync_store_entitlement(store))
        self.stdout.write(self.style.SUCCESS(
            f"Reconciled {total} stores; {premium} Premium; "
            f"expired {expired_subscriptions} subscriptions."
        ))
