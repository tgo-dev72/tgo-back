"""Email rendering + sending for tenant-scoped newsletters.

Ported from legacy store/email_service.py with the platform rule applied:
templates receive a *store context* (StoreConfig branding + from-email)
injected at send time — no company identity is baked into the template.
"""
import logging
import uuid
from datetime import timedelta
from email.utils import make_msgid
from urllib.parse import urlencode
from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.service import email_context

from .models import CampaignDelivery, NewsletterCampaign, Subscriber

logger = logging.getLogger(__name__)

UNSUBSCRIBE_SIGNING_SALT = "tijarago.marketing.unsubscribe.v1"


def unsubscribe_urls(store, email, language="en"):
    """Return the branded customer link and the RFC 8058 one-click endpoint."""
    email = email.lower().strip()
    subscriber = Subscriber.objects.filter(store=store, email=email).only("id").first()
    public_base = getattr(settings, "UNSUBSCRIBE_BASE_URL", "").strip()
    if not public_base:
        public_base = f"{settings.PLATFORM_WEBSITE_URL.rstrip('/')}/unsubscribe"
    if subscriber:
        public_url = f"{public_base.rstrip('/')}/{subscriber.pk}/?{urlencode({'lang': language})}"
        one_click_url = f"{settings.PLATFORM_WEBSITE_URL.rstrip('/')}/api/unsubscribe/{subscriber.pk}"
        return public_url, one_click_url

    # Test deliveries can target an address outside the audience. Keep them
    # functional without exposing the address itself in either URL.
    token = signing.dumps(
        {"store_id": store.pk, "email": email},
        salt=UNSUBSCRIBE_SIGNING_SALT,
        compress=True,
    )
    query = urlencode({"token": token, "lang": language})
    public_url = f"{public_base.rstrip('/')}/?{query}"
    one_click_url = f"{settings.STOREFRONT_API_URL.rstrip('/')}/api/unsubscribe/?{urlencode({'token': token})}"
    return public_url, one_click_url


def unsubscribe_url(store, email, language="en"):
    return unsubscribe_urls(store, email, language)[0]


EMAIL_COPY = {
    "en": {
        "eyebrow": "Newsletter",
        "reason": "You're receiving this because you subscribed to {brand} updates.",
        "unsubscribe": "Unsubscribe",
        "text_unsubscribe": "To unsubscribe, visit:",
        "rights": "All rights reserved.",
    },
    "fr": {
        "eyebrow": "Newsletter",
        "reason": "Vous recevez cet e-mail car vous êtes abonné aux actualités de {brand}.",
        "unsubscribe": "Se désabonner",
        "text_unsubscribe": "Pour vous désabonner, consultez :",
        "rights": "Tous droits réservés.",
    },
    "ar": {
        "eyebrow": "النشرة البريدية",
        "reason": "تتوصل بهذه الرسالة لأنك اشتركت في أخبار {brand}.",
        "unsubscribe": "إلغاء الاشتراك",
        "text_unsubscribe": "لإلغاء الاشتراك، زر:",
        "rights": "جميع الحقوق محفوظة.",
    },
}


def build_html_email(subject, preview_text, body_html, unsubscribe_url, brand_name, brand_color, language="en"):
    year = timezone.now().year
    accent = brand_color
    copy = EMAIL_COPY.get(language, EMAIL_COPY["en"])
    direction = "rtl" if language == "ar" else "ltr"
    return f"""<!DOCTYPE html>
<html lang="{language}" dir="{direction}">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{subject}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background-color: #F5F4F0; font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
            font-size: 16px; line-height: 1.7; color: #2C2C2A; -webkit-font-smoothing: antialiased; }}
    .email-wrapper {{ padding: 40px 16px; }}
    .email-container {{ max-width: 600px; margin: 0 auto; }}
    .email-header {{ background-color: {accent}; border-radius: 16px 16px 0 0; padding: 32px 40px;
                     text-align: center; }}
    .email-logo {{ font-size: 20px; font-weight: 600; color: #FFFFFF; letter-spacing: -0.5px; }}
    .email-hero {{ background-color: #FFFFFF; padding: 48px 40px 40px; border-left: 1px solid #E8E6E0;
                   border-right: 1px solid #E8E6E0; }}
    .email-eyebrow {{ display: inline-block; font-size: 12px; font-weight: 500; letter-spacing: 1.5px;
                      text-transform: uppercase; color: {accent}; background-color: #F0F4F4;
                      padding: 4px 12px; border-radius: 100px; margin-bottom: 20px; }}
    .email-subject {{ font-size: 28px; font-weight: 600; line-height: 1.3; color: #1A1A18; margin-bottom: 12px; }}
    .email-preview {{ font-size: 15px; color: #888780; margin-bottom: 0; }}
    .email-divider {{ background-color: #FFFFFF; padding: 0 40px; border-left: 1px solid #E8E6E0;
                      border-right: 1px solid #E8E6E0; }}
    .email-divider hr {{ border: none; border-top: 1px solid #ECEAE4; margin: 0; }}
    .email-body {{ background-color: #FFFFFF; padding: 36px 40px 48px; border-left: 1px solid #E8E6E0;
                   border-right: 1px solid #E8E6E0; font-size: 16px; line-height: 1.8; color: #2C2C2A; }}
    .email-body h2 {{ font-size: 20px; font-weight: 600; color: #1A1A18; margin: 28px 0 10px; }}
    .email-body p {{ margin-bottom: 16px; color: #444441; }}
    .email-body a {{ color: {accent}; text-decoration: underline; }}
    .email-cta-wrapper {{ text-align: center; margin: 32px 0 8px; }}
    .email-cta {{ display: inline-block; background-color: {accent}; color: #FFFFFF !important;
                  text-decoration: none !important; font-size: 15px; font-weight: 500;
                  padding: 14px 32px; border-radius: 8px; }}
    .email-footer {{ background-color: #ECEAE4; border-radius: 0 0 16px 16px; padding: 28px 40px;
                     text-align: center; border-left: 1px solid #E8E6E0; border-right: 1px solid #E8E6E0;
                     border-bottom: 1px solid #E8E6E0; }}
    .email-footer p {{ font-size: 12px; color: #888780; margin-bottom: 6px; }}
    .email-footer a {{ color: #5F5E5A; text-decoration: underline; font-size: 12px; }}
    @media (max-width: 620px) {{
      .email-wrapper {{ padding: 20px 12px; }}
      .email-header, .email-hero, .email-body, .email-footer {{ padding-left: 24px; padding-right: 24px; }}
    }}
  </style>
</head>
<body>
  <div class="email-wrapper">
    <div class="email-container">
      <div class="email-header">
        <span class="email-logo">{brand_name}</span>
      </div>
      <div class="email-hero">
        <span class="email-eyebrow">{copy['eyebrow']}</span>
        <h1 class="email-subject">{subject}</h1>
        {f'<p class="email-preview">{preview_text}</p>' if preview_text else ''}
      </div>
      <div class="email-divider"><hr /></div>
      <div class="email-body">{body_html}</div>
      <div class="email-footer">
        <p>{copy['reason'].format(brand=brand_name)}</p>
        <p><a href="{unsubscribe_url}">{copy['unsubscribe']}</a></p>
        <p style="margin-top: 10px;">&copy; {year} {brand_name}. {copy['rights']}</p>
      </div>
    </div>
  </div>
</body>
</html>"""


def build_text_email(subject, body_text, unsubscribe_url, brand_name, language="en"):
    copy = EMAIL_COPY.get(language, EMAIL_COPY["en"])
    return f"""{subject}
{"=" * len(subject)}

{body_text}

---
{copy['text_unsubscribe']} {unsubscribe_url}
© {timezone.now().year} {brand_name}
"""


def _send_recipient(campaign, email):
    store = campaign.store
    ctx = email_context(store)
    recipient_unsubscribe_url, one_click_unsubscribe_url = unsubscribe_urls(store, email, campaign.language)
    try:
        html_content = build_html_email(
            subject=campaign.subject, preview_text=campaign.preview_text,
            body_html=campaign.body_html, unsubscribe_url=recipient_unsubscribe_url,
            brand_name=ctx["brand_name"], brand_color=ctx["brand_color"], language=campaign.language,
        )
        text_content = build_text_email(
            subject=campaign.subject, body_text=campaign.body_text or "",
            unsubscribe_url=recipient_unsubscribe_url, brand_name=ctx["brand_name"], language=campaign.language,
        )
        message_id = make_msgid(domain=settings.STORE_EMAIL_DOMAIN)
        msg = EmailMultiAlternatives(
            subject=campaign.subject, body=text_content,
            from_email=f"{ctx['from_name']} <{ctx['from_email']}>", to=[email],
            headers={
                "List-Unsubscribe": f"<{one_click_unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                "Message-ID": message_id,
            },
        )
        msg.attach_alternative(html_content, "text/html")
        msg.send()
    except Exception as exc:  # noqa: BLE001
        logger.error("Campaign delivery failed: %s", type(exc).__name__)
        return False, str(exc)[:500]
    try:
        from store_mail.services import record_outbound
        record_outbound(
            store, to=email, subject=campaign.subject, text=text_content,
            html=html_content, message_id=message_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Campaign delivery was sent but could not be recorded in the store mailbox")
    return True, ""


def prepare_campaign_deliveries(campaign):
    recipients = list(
        Subscriber.objects.filter(store=campaign.store, status=Subscriber.Status.ACTIVE)
        .values_list("email", flat=True)
    )
    task_id = uuid.uuid4()
    with transaction.atomic():
        campaign = NewsletterCampaign.objects.select_for_update().get(pk=campaign.pk)
        campaign.deliveries.all().delete()
        CampaignDelivery.objects.bulk_create(
            [CampaignDelivery(campaign=campaign, email=email) for email in recipients],
            batch_size=500,
        )
        campaign.delivery_task_id = task_id
        campaign.recipients_count = len(recipients)
        campaign.sent_count = 0
        campaign.failed_count = 0
        campaign.status = NewsletterCampaign.Status.SENDING
        campaign.save(update_fields=[
            "delivery_task_id", "recipients_count", "sent_count", "failed_count", "status", "updated_at",
        ])
    return task_id, len(recipients)


def send_campaign_batch(campaign_id, *, task_id, batch_size=25):
    campaign = NewsletterCampaign.objects.select_related("store").get(pk=campaign_id)
    if str(campaign.delivery_task_id) != str(task_id):
        return {"terminal": True, "status": "superseded"}
    stale = timezone.now() - timedelta(minutes=10)
    with transaction.atomic():
        deliveries = list(
            CampaignDelivery.objects.select_for_update(skip_locked=True)
            .filter(campaign=campaign, attempts__lt=3)
            .filter(Q(status=CampaignDelivery.Status.PENDING) | Q(status=CampaignDelivery.Status.PROCESSING, started_at__lt=stale))
            .order_by("id")[:batch_size]
        )
        claimed_at = timezone.now()
        for delivery in deliveries:
            delivery.status = CampaignDelivery.Status.PROCESSING
            delivery.started_at = claimed_at
            delivery.attempts += 1
        CampaignDelivery.objects.bulk_update(deliveries, ["status", "started_at", "attempts", "updated_at"])

    for delivery in deliveries:
        success, error = _send_recipient(campaign, delivery.email)
        delivery.status = CampaignDelivery.Status.SENT if success else (
            CampaignDelivery.Status.PENDING if delivery.attempts < 3 else CampaignDelivery.Status.FAILED
        )
        delivery.error = error
        delivery.sent_at = timezone.now() if success else None
        delivery.started_at = None
        delivery.save(update_fields=["status", "error", "sent_at", "started_at", "updated_at"])

    sent = campaign.deliveries.filter(status=CampaignDelivery.Status.SENT).count()
    failed = campaign.deliveries.filter(status=CampaignDelivery.Status.FAILED).count()
    remaining = campaign.deliveries.exclude(status__in=[CampaignDelivery.Status.SENT, CampaignDelivery.Status.FAILED]).count()
    NewsletterCampaign.objects.filter(pk=campaign.pk, delivery_task_id=task_id).update(
        sent_count=sent, failed_count=failed,
    )
    if remaining == 0:
        NewsletterCampaign.objects.filter(pk=campaign.pk, delivery_task_id=task_id).update(
            status=NewsletterCampaign.Status.SENT if failed == 0 else NewsletterCampaign.Status.FAILED,
            sent_at=timezone.now(), sent_count=sent, failed_count=failed,
        )
    return {"terminal": remaining == 0, "sent": sent, "failed": failed, "remaining": remaining}


def send_campaign(campaign, test_email=None, *, prepared=False):
    """Send a campaign to the store's active subscribers (or a test address).

    Returns a summary dict with sent/failed counts and updates the campaign
    counters (skipped in test mode)."""
    store = campaign.store
    if test_email:
        recipients = [test_email]
    else:
        recipients = list(
            Subscriber.objects.filter(store=store, status=Subscriber.Status.ACTIVE)
            .values_list("email", flat=True)
        )

    if not recipients:
        if prepared:
            campaign.status = NewsletterCampaign.Status.FAILED
            campaign.recipients_count = 0
            campaign.failed_count = 0
            campaign.save(update_fields=["status", "recipients_count", "failed_count", "updated_at"])
        return {"sent": 0, "failed": 0, "total": 0, "error": "No active subscribers."}

    if not test_email and not prepared:
        campaign.status = NewsletterCampaign.Status.SENDING
        campaign.recipients_count = len(recipients)
        campaign.save(update_fields=["status", "recipients_count", "updated_at"])

    sent, failed = 0, 0
    for email in recipients:
        success, _error = _send_recipient(campaign, email)
        sent += int(success)
        failed += int(not success)

    if not test_email:
        campaign.status = NewsletterCampaign.Status.SENT if failed == 0 else NewsletterCampaign.Status.FAILED
        campaign.sent_at = timezone.now()
        campaign.sent_count = sent
        campaign.failed_count = failed
        campaign.save(update_fields=["status", "sent_at", "sent_count", "failed_count", "updated_at"])

    return {"sent": sent, "failed": failed, "total": len(recipients)}
