from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import MerchantProfile
from store.models import StoreConfig
from subscriptions.models import SubscriptionPayment, SubscriptionPlan, UserSubscription
from subscriptions.services import _release_reserved_credit, ensure_free_subscription


class Command(BaseCommand):
    help = "Reset current merchant access to the protected Free baseline. Dry-run unless --apply is supplied."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the reset. Without this option the command only reports affected rows.",
        )

    def handle(self, *args, **options):
        free_plan = SubscriptionPlan.objects.filter(slug="free").first()
        if free_plan is None:
            self.stderr.write(self.style.ERROR("Cannot reset subscriptions: the protected 'free' plan does not exist."))
            return

        active = UserSubscription.objects.filter(status=UserSubscription.Status.ACTIVE)
        pending = SubscriptionPayment.objects.filter(status=SubscriptionPayment.Status.PENDING)
        premium_stores = StoreConfig.objects.exclude(plan=StoreConfig.Plans.FREE)
        account_ids = list(MerchantProfile.objects.filter(
            user__is_active=True,
            user__email_verified_at__isnull=False,
        ).values_list("user_id", flat=True).distinct())
        counts = {
            "merchant_accounts": len(account_ids),
            "active_subscriptions": active.count(),
            "pending_checkouts": pending.count(),
            "non_free_store_caches": premium_stores.count(),
        }

        mode = "APPLY" if options["apply"] else "DRY RUN"
        self.stdout.write(f"{mode}: reset current merchant access to {free_plan.name} ({free_plan.slug})")
        for label, count in counts.items():
            self.stdout.write(f"  {label}: {count}")

        if not options["apply"]:
            self.stdout.write("No data changed. Run again with --apply after reviewing these counts.")
            return

        now = timezone.now()
        with transaction.atomic():
            canceled_subscriptions = active.update(
                status=UserSubscription.Status.CANCELED,
                cancel_at_period_end=False,
                updated_at=now,
            )
            canceled_checkouts = 0
            for payment in pending.select_for_update().select_related("user"):
                _release_reserved_credit(payment)
                payment.status = SubscriptionPayment.Status.CANCELED
                payment.processed_at = now
                payment.save(update_fields=["status", "processed_at", "updated_at"])
                canceled_checkouts += 1
            reset_stores = premium_stores.update(plan=StoreConfig.Plans.FREE)
            for user_id in account_ids:
                ensure_free_subscription(MerchantProfile.objects.get(user_id=user_id).user)

        self.stdout.write(self.style.SUCCESS(
            "Reset complete: "
            f"{canceled_subscriptions} subscriptions canceled, "
            f"{canceled_checkouts} pending checkouts invalidated, "
            f"{reset_stores} store caches set to Free."
        ))
