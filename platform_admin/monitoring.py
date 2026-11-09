"""Operational alert collection and email delivery."""

import hashlib
import json
from datetime import timedelta

from django.utils import timezone

from marketing.models import NewsletterCampaign
from orders.models import QuantityException
from payments.models import PaymentTransaction
from storefront_factory.models import StorefrontRequest
from notifications.platform_email import send_platform_email

from .models import PlatformMonitoringSettings


def collect_operational_incidents(now=None):
    now = now or timezone.now()
    stalled_states = [
        StorefrontRequest.PIPELINE_ACCEPTED,
        StorefrontRequest.PIPELINE_PREPARING,
        StorefrontRequest.PIPELINE_PUBLISHING,
        StorefrontRequest.PIPELINE_PROVISIONING,
        StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
        StorefrontRequest.PIPELINE_BUILDING,
        StorefrontRequest.PIPELINE_VERIFYING,
    ]
    definitions = [
        ("failed_deployments", "Failed storefront deployments", StorefrontRequest.objects.filter(
            deploy_status=StorefrontRequest.DEPLOY_FAILED,
        ).count()),
        ("stalled_deployments", "Storefront deployments stalled over 6 minutes", StorefrontRequest.objects.filter(
            pipeline_state__in=stalled_states,
            last_heartbeat_at__lt=now - timedelta(minutes=6),
        ).count()),
        ("failed_payments", "Payment failures in the last 24 hours", PaymentTransaction.objects.filter(
            status=PaymentTransaction.Status.FAILED,
            updated_at__gte=now - timedelta(hours=24),
        ).count()),
        ("stock_exceptions", "Untreated stock exceptions", QuantityException.objects.filter(treated=False).count()),
        ("failed_campaigns", "Failed email campaigns", NewsletterCampaign.objects.filter(
            status=NewsletterCampaign.Status.FAILED,
        ).count()),
    ]
    return [
        {"key": key, "label": label, "count": count}
        for key, label, count in definitions if count
    ]


def _signature(incidents):
    payload = json.dumps(incidents, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def send_monitoring_test(settings_record=None):
    record = settings_record or PlatformMonitoringSettings.load()
    delivered = send_platform_email(
        to=record.alert_email,
        subject="TijaraGO monitoring test",
        language="en",
        preheader="Monitoring email delivery is configured.",
        eyebrow="Platform monitoring",
        title="Monitoring test delivered",
        paragraphs=[
            "This message confirms that TijaraGO can deliver operational alerts to this address.",
        ],
        details=[{"label": "Alert recipient", "value": record.alert_email}],
        note="No production incident was created by this test.",
    )
    if delivered:
        record.last_test_sent_at = timezone.now()
        record.save(update_fields=["last_test_sent_at", "updated_at"])
    return delivered


def dispatch_monitoring_alert(now=None):
    now = now or timezone.now()
    record = PlatformMonitoringSettings.load()
    if not record.pk:
        record.save()
    if not record.enabled or not record.alert_email:
        return {"status": "disabled", "delivered": False, "incidents": 0}

    incidents = collect_operational_incidents(now)
    signature = _signature(incidents) if incidents else ""
    reminder_due = (
        record.last_alert_sent_at is None
        or record.last_alert_sent_at <= now - timedelta(minutes=record.reminder_minutes)
    )
    changed = signature != record.last_alert_signature
    if not incidents and not record.last_alert_signature:
        return {"status": "healthy", "delivered": False, "incidents": 0}
    if not changed and not (incidents and reminder_due):
        return {"status": "unchanged", "delivered": False, "incidents": len(incidents)}

    recovered = not incidents
    details = (
        [{"label": item["label"], "value": str(item["count"])} for item in incidents]
        if incidents else [{"label": "Status", "value": "All monitored operational signals are clear"}]
    )
    delivered = send_platform_email(
        to=record.alert_email,
        subject="TijaraGO monitoring recovered" if recovered else f"TijaraGO alert: {len(incidents)} signal(s) need attention",
        language="en",
        preheader="Operational monitoring update from TijaraGO.",
        eyebrow="Platform monitoring",
        title="Monitoring recovered" if recovered else "Operational attention required",
        paragraphs=[
            "The previously reported operational signals are now clear."
            if recovered else "TijaraGO detected operational signals that require review in platform administration."
        ],
        details=details,
        action_url="https://admin.tijarago.com/system",
        action_label="Open system monitoring",
        note="Payment sandbox mode is not changed by monitoring.",
        headers={"X-TijaraGO-Monitoring": signature or "recovered"},
    )
    if delivered:
        record.last_alert_signature = signature
        record.last_alert_sent_at = now
        record.save(update_fields=["last_alert_signature", "last_alert_sent_at", "updated_at"])
    return {"status": "recovered" if recovered else "alert", "delivered": delivered, "incidents": len(incidents)}
