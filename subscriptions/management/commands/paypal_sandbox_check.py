from django.core.management.base import BaseCommand, CommandError

from payments.gateways.paypal import PayPalGateway


class Command(BaseCommand):
    help = "Validate PayPal Sandbox configuration and OAuth without charging anything."

    def handle(self, *args, **options):
        gateway = PayPalGateway()
        credentials = gateway.platform_credentials()
        credentials["sandbox"] = True
        try:
            status = gateway.configuration_status(credentials)
            if not status["configured"]:
                raise CommandError("PayPal Sandbox credentials or receiving-account identity are missing.")
            gateway._access_token(credentials)
        except Exception as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            "PayPal Sandbox configuration and OAuth are valid. No order was created or captured."
        ))
