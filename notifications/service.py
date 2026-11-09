"""Shared, store-agnostic transactional email service.

Every store's email is sent from that store's own identity: the brand name,
brand color and from-address come from StoreConfig and are injected at send
time — never baked into a template. Marketing (newsletters) and any future
notification (order confirmations, delivery updates) reuse this module.
"""
import logging
from email.utils import make_msgid

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from common.media_uploads import media_asset_url

logger = logging.getLogger(__name__)


def email_context(store):
    """Store-scoped send context: brand name/color/from-address."""
    # Query fresh rather than `store.config`: the reverse OneToOne cache on a
    # store instance can be stale (e.g. populated by the create signal before
    # the row was updated).
    from store.email_routing import alias_for
    from store.models import StoreConfig

    config = StoreConfig.objects.get(store=store)
    logo_url = ""
    if store.logo:
        logo_url = media_asset_url(store.logo, base_url=settings.STOREFRONT_API_URL)
    return {
        "brand_name": store.name,
        "brand_color": config.brand_color or "#1F8A8C",
        "logo_url": logo_url,
        "support_email": config.contact_email or config.from_email or alias_for(store),
        "language": config.default_locale if config.default_locale in {"ar", "en", "fr"} else "ar",
        "from_name": store.name,
        # A store may explicitly use another sender that its provider has
        # verified. Otherwise, send from the same stable slug-based address
        # customers use for inbound forwarding.
        "from_email": config.from_email or alias_for(store),
    }


def send_email(store, *, subject, to, html, text="", headers=None, attachments=None):
    """Send a branded email as the given store.

    Returns True on success, False on failure (never raises — a failed
    transactional email must not break the checkout/order request).
    """
    ctx = email_context(store)
    message_id = make_msgid(domain=settings.STORE_EMAIL_DOMAIN)
    message_headers = {**(headers or {}), "Message-ID": message_id}
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=f"{ctx['from_name']} <{ctx['from_email']}>",
            to=[to],
            headers=message_headers,
        )
        msg.attach_alternative(html, "text/html")
        for filename, content, mimetype in attachments or []:
            msg.attach(filename, content, mimetype)
        msg.send()
    except Exception as exc:  # noqa: BLE001
        logger.error("Email to %s failed: %s", to, exc)
        try:
            from store_mail.models import MailMessage
            from store_mail.services import record_outbound
            record_outbound(
                store, to=to, subject=subject, text=text, html=html, message_id=message_id,
                status=MailMessage.Status.FAILED,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record unsuccessful store email")
        return False
    try:
        from store_mail.services import record_outbound
        record_outbound(
            store, to=to, subject=subject, text=text, html=html, message_id=message_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Email sent to %s but could not be recorded in the store mailbox", to)
    return True


def _money(value, currency):
    return f"{value:,.2f} {currency}"


def _payment_label(order, language):
    labels = {
        "ar": {"cod": "الدفع عند الاستلام", "paid": "تم الدفع بأمان", "pending": "الدفع قيد الانتظار"},
        "en": {"cod": "Cash on delivery", "paid": "Paid securely", "pending": "Payment pending"},
        "fr": {"cod": "Paiement à la livraison", "paid": "Paiement sécurisé", "pending": "Paiement en attente"},
    }[language]
    if order.payment_mode == order.PaymentMode.COD:
        return labels["cod"]
    if order.is_paid == order.IsPaid.CONFIRMED:
        return labels["paid"]
    return labels["pending"]


def _order_email_content(order, event, language):
    brand_name = order.store.name
    english = {
        "received": {
            "subject": f"Your order is with us · {brand_name}",
            "eyebrow": "Order received",
            "title": "Thank you. We have your order.",
            "preheader": "Your order has arrived and will be reviewed with care.",
            "paragraphs": [
                "Your selection has reached our team and is now awaiting confirmation.",
                "No online payment has been collected. We will write again as soon as everything is confirmed.",
            ],
            "note": "We will review availability before confirming your order.",
        },
        "confirmed": {
            "subject": f"Your order is confirmed · {brand_name}",
            "eyebrow": "Order confirmed",
            "title": "Your selection is confirmed.",
            "preheader": "Everything is in place. We are preparing your order with care.",
            "paragraphs": [
                "Everything is in place, and your order is now being prepared.",
                "We will keep you informed as it moves through each considered step.",
            ],
            "note": "A further update will arrive when your order is on its way.",
        },
        "shipped": {
            "subject": f"Your order is on its way · {brand_name}",
            "eyebrow": "On its way",
            "title": "Your order has left us.",
            "preheader": "Your order is on its way to you.",
            "paragraphs": [
                "Your order has been carefully prepared and is now on its way.",
                "We will let you know once it has been delivered.",
            ],
            "note": "Tracking updates can take a little time to appear after dispatch.",
        },
        "delivered": {
            "subject": f"Delivered with care · {brand_name}",
            "eyebrow": "Delivered",
            "title": "Delivered, with our compliments.",
            "preheader": "Your order has arrived. We hope you enjoy every detail.",
            "paragraphs": [
                "Your order has been delivered.",
                f"We hope you enjoy your selection, and we thank you for choosing {brand_name}.",
            ],
            "note": "If anything needs our attention, simply reply to this email.",
        },
        "cancelled": {
            "subject": f"An update to your order · {brand_name}",
            "eyebrow": "Order update",
            "title": "Your order has been cancelled.",
            "preheader": "An important update regarding your recent order.",
            "paragraphs": [
                "Your order has been cancelled and will not move forward.",
                "If you would like any help, our team will be pleased to assist you.",
            ],
            "note": order.cancel_reason or "Reply to this email if you would like further details.",
        },
    }
    arabic = {
        "received": {
            "subject": f"طلبك بين أيدينا · {brand_name}",
            "eyebrow": "تم استلام الطلب",
            "title": "شكرًا لك. لقد استلمنا طلبك.",
            "preheader": "وصل طلبك وسنراجعه بكل عناية.",
            "paragraphs": ["وصل اختيارك إلى فريقنا، وهو الآن في انتظار التأكيد.", "لم يتم تحصيل أي دفعة إلكترونية. سنراسلك فور تأكيد كل التفاصيل."],
            "note": "سنتحقق من توفر المنتجات قبل تأكيد طلبك.",
        },
        "confirmed": {
            "subject": f"تم تأكيد طلبك · {brand_name}",
            "eyebrow": "تم تأكيد الطلب",
            "title": "تم تأكيد اختيارك.",
            "preheader": "كل شيء جاهز، ونحضّر طلبك بعناية.",
            "paragraphs": ["كل شيء جاهز، وطلبك الآن قيد التحضير.", "سنبقيك على اطلاع مع انتقاله عبر كل مرحلة."],
            "note": "سنرسل إليك تحديثًا آخر عندما يصبح طلبك في الطريق.",
        },
        "shipped": {
            "subject": f"طلبك في الطريق · {brand_name}",
            "eyebrow": "في الطريق",
            "title": "طلبك في طريقه إليك.",
            "preheader": "تم تجهيز طلبك بعناية وهو الآن في الطريق.",
            "paragraphs": ["تم تجهيز طلبك بعناية، وهو الآن في طريقه إليك.", "سنخبرك فور تسليمه."],
            "note": "قد تستغرق تحديثات التتبع بعض الوقت بعد الشحن.",
        },
        "delivered": {
            "subject": f"تم التسليم بكل عناية · {brand_name}",
            "eyebrow": "تم التسليم",
            "title": "وصل طلبك بكل عناية.",
            "preheader": "وصل طلبك. نتمنى أن تستمتع بكل تفاصيله.",
            "paragraphs": ["تم تسليم طلبك.", f"نتمنى أن تستمتع باختيارك، ونشكرك على اختيار {brand_name}."],
            "note": "إذا احتجت إلى أي مساعدة، ما عليك سوى الرد على هذه الرسالة.",
        },
        "cancelled": {
            "subject": f"تحديث بخصوص طلبك · {brand_name}",
            "eyebrow": "تحديث الطلب",
            "title": "تم إلغاء طلبك.",
            "preheader": "تحديث مهم بخصوص طلبك الأخير.",
            "paragraphs": ["تم إلغاء طلبك ولن تتم متابعته.", "إذا احتجت إلى أي مساعدة، سيسعد فريقنا بخدمتك."],
            "note": order.cancel_reason or "يمكنك الرد على هذه الرسالة لمعرفة المزيد من التفاصيل.",
        },
    }
    french = {
        "received": {"subject": f"Votre commande est entre nos mains · {brand_name}", "eyebrow": "Commande reçue", "title": "Merci. Nous avons bien reçu votre commande.", "preheader": "Votre commande est arrivée et sera examinée avec soin.", "paragraphs": ["Votre sélection est parvenue à notre équipe et attend maintenant sa confirmation.", "Aucun paiement en ligne n'a été prélevé. Nous vous écrirons dès que tout sera confirmé."], "note": "Nous vérifierons la disponibilité avant de confirmer votre commande."},
        "confirmed": {"subject": f"Votre commande est confirmée · {brand_name}", "eyebrow": "Commande confirmée", "title": "Votre sélection est confirmée.", "preheader": "Tout est en place. Nous préparons votre commande avec soin.", "paragraphs": ["Tout est en place et votre commande est en cours de préparation.", "Nous vous tiendrons informé à chaque étape."], "note": "Vous recevrez une nouvelle mise à jour lorsque votre commande sera en route."},
        "shipped": {"subject": f"Votre commande est en route · {brand_name}", "eyebrow": "En route", "title": "Votre commande est partie.", "preheader": "Votre commande est en route vers vous.", "paragraphs": ["Votre commande a été préparée avec soin et est maintenant en route.", "Nous vous informerons dès qu'elle aura été livrée."], "note": "Les informations de suivi peuvent prendre un peu de temps après l'expédition."},
        "delivered": {"subject": f"Livrée avec soin · {brand_name}", "eyebrow": "Livrée", "title": "Livrée, avec nos compliments.", "preheader": "Votre commande est arrivée. Profitez de chaque détail.", "paragraphs": ["Votre commande a été livrée.", f"Nous espérons que votre sélection vous plaira et vous remercions d'avoir choisi {brand_name}."], "note": "Si quelque chose requiert notre attention, répondez simplement à cet e-mail."},
        "cancelled": {"subject": f"Une mise à jour de votre commande · {brand_name}", "eyebrow": "Mise à jour", "title": "Votre commande a été annulée.", "preheader": "Une information importante concernant votre commande récente.", "paragraphs": ["Votre commande a été annulée et ne sera pas poursuivie.", "Si vous avez besoin d'aide, notre équipe se fera un plaisir de vous assister."], "note": order.cancel_reason or "Répondez à cet e-mail pour obtenir plus de détails."},
    }
    copy = {"ar": arabic, "en": english, "fr": french}[language][event]
    if event == "shipped" and order.tracking_number:
        carrier = f" · {order.carrier}" if order.carrier else ""
        tracking_label = {"ar": "رقم التتبع", "en": "Tracking", "fr": "Suivi"}[language]
        copy["note"] = f"{tracking_label} {order.tracking_number}{carrier}"
    return copy


def _send_order_email(order, event):
    if not order.customer_email:
        return False
    brand = email_context(order.store)
    language = brand["language"]
    content = _order_email_content(order, event, language)
    labels = {
        "ar": {"greeting": "عزيزي", "reference_label": "مرجع الطلب", "payment": "الدفع", "selection": "اختياراتك", "quantity": "الكمية", "subtotal_label": "المجموع الفرعي", "discount_label": "الخصم الخاص", "delivery_label": "التوصيل", "total_label": "الإجمالي", "address_label": "عنوان التوصيل", "questions": "لديك استفسار؟ يمكنك الرد على هذه الرسالة أو مراسلتنا على", "customer_fallback": "عميلنا العزيز"},
        "en": {"greeting": "Dear", "reference_label": "Order reference", "payment": "Payment", "selection": "Your selection", "quantity": "Quantity", "subtotal_label": "Subtotal", "discount_label": "Private offer", "delivery_label": "Delivery", "total_label": "Total", "address_label": "Delivery address", "questions": "Questions? Reply to this email or write to", "customer_fallback": "there"},
        "fr": {"greeting": "Bonjour", "reference_label": "Référence de commande", "payment": "Paiement", "selection": "Votre sélection", "quantity": "Quantité", "subtotal_label": "Sous-total", "discount_label": "Offre privée", "delivery_label": "Livraison", "total_label": "Total", "address_label": "Adresse de livraison", "questions": "Une question ? Répondez à cet e-mail ou écrivez-nous à", "customer_fallback": "cher client"},
    }[language]
    items = []
    for item in order.items.all():
        quantity = item.fulfilled_quantity or item.quantity
        items.append({
            "name": item.name,
            "quantity": quantity,
            "unit_price": _money(item.price, order.currency),
            "line_total": _money(item.price * quantity, order.currency),
        })
    reference = f"#{str(order.order_id).split('-', 1)[0].upper()}"
    context = {
        **brand,
        **content,
        **labels,
        "direction": "rtl" if language == "ar" else "ltr",
        "text_align": "right" if language == "ar" else "left",
        "opposite_align": "left" if language == "ar" else "right",
        "body_font": "'Noto Sans Arabic',Tahoma,Arial,sans-serif" if language == "ar" else "Arial,sans-serif",
        "display_font": "'Noto Sans Arabic',Tahoma,Arial,sans-serif" if language == "ar" else "Georgia,serif",
        "greeting_suffix": "،" if language == "ar" else ",",
        "customer_name": order.customer_name or labels["customer_fallback"],
        "order_reference": reference,
        "items": items,
        "payment_label": _payment_label(order, language),
        "subtotal_value": _money(order.subtotal, order.currency),
        "shipping_value": _money(order.shipping_amount, order.currency),
        "discount_value": _money(order.discount_amount, order.currency) if order.discount_amount else "",
        "total_value": _money(order.amount, order.currency),
        "delivery_address": ", ".join(
            part for part in (order.customer_address.strip(), order.customer_city.strip()) if part
        ),
    }
    return send_email(
        order.store,
        subject=content["subject"],
        to=order.customer_email,
        html=render_to_string("notifications/order_email.html", context),
        text=render_to_string("notifications/order_email.txt", context),
        headers={"X-Entity-Ref-ID": str(order.order_id)},
    )


def send_order_received(order):
    """Acknowledge a placed COD order without claiming merchant confirmation or payment."""
    return _send_order_email(order, "received")


def send_order_confirmation(order):
    """Transactional email sent when an order is confirmed (webhook/COD).

    Demonstrates the store-agnostic pattern: only the order's own snapshot
    fields and the store's branding are used — no global company identity.
    Returns True if the email was sent."""
    return _send_order_email(order, "confirmed")


def send_order_shipped(order):
    """Transactional email sent when an order is shipped."""
    return _send_order_email(order, "shipped")


def send_order_delivered(order):
    """Transactional email sent when an order is delivered."""
    return _send_order_email(order, "delivered")


def send_order_cancelled(order):
    """Transactional email sent when an order is cancelled."""
    return _send_order_email(order, "cancelled")


def send_invoice_email(invoice):
    """Deliver the already-issued original; retries never create a new number or PDF."""
    from django.conf import settings
    from django.core import signing
    from django.utils import timezone
    from orders.invoices import signed_invoice_url
    from orders.models import Invoice

    order = invoice.order
    if not order.customer_email or invoice.status != Invoice.Status.READY:
        return False
    invoice.delivery_attempts += 1
    invoice.save(update_fields=["delivery_attempts", "updated_at"])
    try:
        if invoice.pdf:
            invoice.pdf.open("rb")
            pdf_bytes = invoice.pdf.read()
            invoice.pdf.close()
        else:
            import requests
            response = requests.get(signed_invoice_url(invoice), timeout=30)
            response.raise_for_status()
            pdf_bytes = response.content
        language = email_context(order.store)["language"]
        copy = {
            "fr": (f"Votre facture {invoice.number}", "Votre facture est jointe à ce message."),
            "en": (f"Your invoice {invoice.number}", "Your invoice is attached to this message."),
            "ar": (f"فاتورتك {invoice.number}", "ستجد فاتورتك مرفقة بهذه الرسالة."),
        }[language]
        guest_link = ""
        if order.client_id is None:
            token = signing.dumps({"order": str(order.order_id), "email": order.customer_email}, salt="guest-invoice")
            from urllib.parse import quote
            guest_link = f"{settings.STOREFRONT_API_URL}/api/storefront/{order.store.slug}/orders/{order.order_id}/invoice/?token={quote(token)}"
        html = f"<p>{copy[1]}</p>" + (f'<p><a href="{guest_link}">Download invoice</a> · link valid for 7 days</p>' if guest_link else "")
        text = copy[1] + (f"\n{guest_link}" if guest_link else "")
        sent = send_email(
            order.store, subject=copy[0], to=order.customer_email,
            html=html, text=text,
            headers={"X-Entity-Ref-ID": f"invoice:{invoice.pk}"},
            attachments=[(f"{invoice.number}.pdf", pdf_bytes, "application/pdf")],
        )
        invoice.delivery_status = Invoice.DeliveryStatus.SENT if sent else Invoice.DeliveryStatus.FAILED
        invoice.delivered_at = timezone.now() if sent else None
        invoice.last_error = "" if sent else "Transactional email delivery failed."
    except Exception as exc:
        invoice.delivery_status = Invoice.DeliveryStatus.FAILED
        invoice.last_error = str(exc)[:2000]
        sent = False
    invoice.save(update_fields=["delivery_status", "delivered_at", "last_error", "updated_at"])
    return sent
