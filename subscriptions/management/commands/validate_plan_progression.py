from django.core.management.base import BaseCommand, CommandError

from subscriptions.models import SubscriptionPlan
from subscriptions.progression import compare_plan_progression


class Command(BaseCommand):
    help = "Validate declared upgrade predecessors or an explicit comma-separated plan path."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            help="Explicit lower-to-higher plan slugs, for example free,starter,medium,prem.",
        )

    def handle(self, *args, **options):
        if options["path"]:
            slugs = [item.strip() for item in options["path"].split(",") if item.strip()]
            plans = {item.slug: item for item in SubscriptionPlan.objects.filter(slug__in=slugs)}
            missing = [slug for slug in slugs if slug not in plans]
            if missing:
                raise CommandError(f"Unknown plan slugs: {', '.join(missing)}")
            pairs = [(plans[lower], plans[higher]) for lower, higher in zip(slugs, slugs[1:])]
        else:
            pairs = [
                (plan.upgrade_from, plan)
                for plan in SubscriptionPlan.objects.select_related("upgrade_from").filter(upgrade_from__isnull=False)
            ]
        conflicts = [
            conflict
            for lower, higher in pairs
            for conflict in compare_plan_progression(lower, higher)
        ]
        if conflicts:
            for conflict in conflicts:
                self.stderr.write(conflict.message())
            raise CommandError(f"Plan progression has {len(conflicts)} conflict(s).")
        self.stdout.write(self.style.SUCCESS(f"Validated {len(pairs)} explicit upgrade step(s)."))
