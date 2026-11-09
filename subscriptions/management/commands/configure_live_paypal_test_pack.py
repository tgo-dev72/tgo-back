import json
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from payments.gateways.availability import effective_platform_credentials
from payments.gateways.registry import get_gateway
from payments.models import PlatformPaymentGateway
from subscriptions.entitlements import configure_plan_entitlements, plan_entitlement_snapshot
from subscriptions.models import SubscriptionPlan


SLUG = "paypal-live-test-001"


class Command(BaseCommand):
    help = "Configure an explicitly named USD 0.01 PayPal-live subscription test pack."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply changes; default is dry-run.")
        parser.add_argument(
            "--plan-only",
            action="store_true",
            help="Create the test plan without changing payment-gateway configuration.",
        )

    def handle(self, *args, **options):
        gateway = get_gateway("paypal")
        credentials = effective_platform_credentials(gateway, setting=None)
        credentials["sandbox"] = False
        status = gateway.configuration_status(credentials)
        report = {
            "mode": "apply" if options["apply"] else "dry-run",
            "plan": {"slug": SLUG, "price": "0.01", "currency": "USD", "duration_days": 1},
            "paypal_live_configured": bool(status["configured"]),
            "paypal_live_webhook_configured": bool(status["webhook_configured"]),
        }
        plan_only = bool(options["plan_only"])
        report["plan_only"] = plan_only
        if not status["configured"] and not plan_only:
            raise CommandError("PayPal live checkout credentials and receiver identity are incomplete; no changes applied.")
        if not options["apply"]:
            self.stdout.write(json.dumps(report, indent=2))
            return

        with transaction.atomic():
            plan, created = SubscriptionPlan.objects.select_for_update().get_or_create(
                slug=SLUG,
                defaults={
                    "name": "PayPal Live Test — $0.01",
                    "description": "One-day live PayPal checkout test pack.",
                    "translations": {
                        "en": {"name": "PayPal Live Test — $0.01", "description": "One-day live PayPal checkout test pack."},
                        "fr": {"name": "Test PayPal réel — 0,01 $", "description": "Pack d’un jour pour tester un paiement PayPal réel."},
                        "ar": {"name": "اختبار PayPal الحقيقي — 0.01 $", "description": "باقة ليوم واحد لاختبار دفع PayPal حقيقي."},
                    },
                    "price": Decimal("0.01"),
                    "currency": "USD",
                    "duration_days": 1,
                    "entitlement_scope": SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
                    "features": ["Live PayPal checkout test"],
                    "is_active": True,
                    "is_featured": False,
                    "sort_order": 999,
                },
            )
            expected = (plan.price, plan.currency, plan.duration_days, plan.entitlement_scope)
            required = (Decimal("0.01"), "USD", 1, SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES)
            if not created and expected != required:
                raise CommandError("The existing test-pack slug has different commercial terms; no changes applied.")
            if created:
                configure_plan_entitlements(
                    plan, plan_entitlement_snapshot(plan), actor=None, increment_revision=False,
                )
            if not plan_only:
                PlatformPaymentGateway.objects.update_or_create(
                    code="paypal", defaults={"is_enabled": True, "sandbox_enabled": False},
                )
                PlatformPaymentGateway.objects.update_or_create(
                    code="youcanpay", defaults={"is_enabled": False},
                )
        report.update(created=created, applied=True)
        self.stdout.write(json.dumps(report, indent=2))
