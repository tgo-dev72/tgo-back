from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from notifications.platform_email import send_platform_email


class Command(BaseCommand):
    help = "Send the responsive TijaraGO email preview to TEST_MAIL."

    def add_arguments(self, parser):
        parser.add_argument("--to", dest="recipient", help="Override TEST_MAIL for this test only.")

    def handle(self, *args, **options):
        recipient = (options.get("recipient") or settings.TEST_MAIL).strip()
        if not recipient:
            raise CommandError("Configure TEST_MAIL or pass --to before sending a test email.")
        try:
            validate_email(recipient)
        except ValidationError as exc:
            raise CommandError("The configured test recipient is not a valid email address.") from exc

        delivered = send_platform_email(
            to=recipient,
            subject="A refined TijaraGO experience, made for your business",
            preheader="Discover the new visual identity of your TijaraGO messages.",
            eyebrow="A private preview",
            title="Your commerce, beautifully in motion",
            recipient_name="TijaraGO team",
            paragraphs=[
                "Every detail of this message has been shaped to feel calm, clear, and distinctly TijaraGO.",
                "The experience follows your merchants from their first store to every important moment in their membership journey.",
            ],
            details=[
                {"label": "Experience", "value": "Premium and responsive"},
                {"label": "Identity", "value": settings.PLATFORM_NAME},
                {"label": "Purpose", "value": "Your business, beautifully presented"},
            ],
            action_url=settings.MERCHANT_DASHBOARD_URL,
            action_label="Enter your workspace",
            note="This private preview was sent from your TijaraGO platform configuration.",
            headers={"X-TijaraGO-Event": "platform_email.preview"},
        )
        if not delivered:
            raise CommandError("The test email could not be delivered. Review the SMTP configuration and server logs.")
        self.stdout.write(self.style.SUCCESS("Test platform email sent to the configured recipient."))
