from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from store_mail.models import MailConversation


class Command(BaseCommand):
    help = "Delete store mailbox conversations older than the configured retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.MAILBOX_RETENTION_DAYS)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = max(1, options["days"])
        cutoff = timezone.now() - timedelta(days=days)
        queryset = MailConversation.objects.filter(last_message_at__lt=cutoff)
        count = queryset.count()
        if not options["dry_run"]:
            queryset.delete()
        self.stdout.write(f"{'Would delete' if options['dry_run'] else 'Deleted'} {count} conversation(s).")
