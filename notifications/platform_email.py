"""Branded transactional email helpers for TijaraGO merchant accounts."""

import logging
import math
import re

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from common.media_uploads import media_asset_url

logger = logging.getLogger(__name__)

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def platform_email_context(**content):
    """Return the stable rendering contract shared by all platform emails."""
    def safe_color(setting_name, fallback):
        value = getattr(settings, setting_name, fallback)
        return value if isinstance(value, str) and HEX_COLOR.fullmatch(value) else fallback

    brand_color = safe_color("PLATFORM_BRAND_COLOR", "#1F8A8C")
    brand_accent = safe_color("PLATFORM_BRAND_ACCENT_COLOR", "#20C6C8")
    brand_ink = safe_color("PLATFORM_BRAND_INK_COLOR", "#073B3C")
    brand_gold = safe_color("PLATFORM_BRAND_GOLD_COLOR", "#D5B36A")
    language = content.pop("language", "ar")
    chrome = {
        "en": {"hello": "Hello", "summary": "At a glance", "help": "Need a hand?", "visit": "Visit TijaraGO"},
        "fr": {"hello": "Bonjour", "summary": "En un coup d’œil", "help": "Besoin d’aide ?", "visit": "Visiter TijaraGO"},
        "ar": {"hello": "مرحبًا", "summary": "لمحة سريعة", "help": "هل تحتاج إلى مساعدة؟", "visit": "زيارة TijaraGO"},
    }.get(language, {})
    return {
        "brand_name": settings.PLATFORM_NAME,
        "brand_tagline": settings.PLATFORM_TAGLINE,
        "brand_color": brand_color,
        "brand_accent": brand_accent,
        "brand_ink": brand_ink,
        "brand_gold": brand_gold,
        "logo_url": settings.PLATFORM_EMAIL_LOGO_URL,
        "website_url": settings.PLATFORM_WEBSITE_URL,
        "support_email": settings.PLATFORM_SUPPORT_EMAIL,
        "language": language,
        "direction": "rtl" if language == "ar" else "ltr",
        "text_align": "right" if language == "ar" else "left",
        "body_font": "'Noto Sans Arabic',Tahoma,Arial,sans-serif" if language == "ar" else "Arial,sans-serif",
        "display_font": "'Noto Sans Arabic',Tahoma,Arial,sans-serif" if language == "ar" else "Georgia,serif",
        "greeting_suffix": "،" if language == "ar" else ",",
        "hello_label": chrome.get("hello", "Hello"),
        "summary_label": chrome.get("summary", "At a glance"),
        "help_label": chrome.get("help", "Need a hand?"),
        "visit_label": chrome.get("visit", "Visit TijaraGO"),
        "details": [],
        "paragraphs": [],
        "action_url": "",
        "action_label": "",
        "note": "",
        **content,
    }


def render_platform_email(**content):
    """Render matching HTML and plain-text bodies from explicit business fields."""
    context = platform_email_context(**content)
    return (
        render_to_string("notifications/platform_email.html", context),
        render_to_string("notifications/platform_email.txt", context),
    )


def send_platform_email(*, to, subject, headers=None, **content):
    """Send one platform email without allowing SMTP failure to break a user flow."""
    html, text = render_platform_email(subject=subject, **content)
    try:
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=f"{settings.PLATFORM_NAME} <{settings.DEFAULT_FROM_EMAIL}>",
            to=[to],
            reply_to=[settings.PLATFORM_SUPPORT_EMAIL] if settings.PLATFORM_SUPPORT_EMAIL else None,
            headers=headers or {},
        )
        message.attach_alternative(html, "text/html")
        return message.send() == 1
    except Exception as exc:  # noqa: BLE001 - notifications must fail safely
        logger.error("Platform email delivery failed: %s", exc)
        return False


def send_subscription_lifecycle_notice(subscription, event):
    """Warn a merchant before expiry and explain the storefront pause at expiry."""
    user = subscription.user
    profile = getattr(user, "merchant_profile", None)
    language = getattr(profile, "preferred_language", "en")
    plan_name = subscription.plan.name if subscription.plan else "TijaraGO"
    dashboard_url = settings.MERCHANT_DASHBOARD_URL.rstrip("/") + "/subscription"
    copies = {
        "en": {
            "seven_days": ("Your pack expires in 7 days", "A friendly renewal reminder", "We would like to remind you that your current pack expires in seven days. If you would like to keep your dashboard and customer storefront available without interruption, you can renew at your convenience."),
            "three_days": ("Your pack expires in 3 days", "A reminder about your pack", "Your current pack expires in three days. To keep your merchant features and public storefront available without interruption, you can complete your renewal whenever it is convenient for you."),
            "expired": ("Your pack has expired", "Your store is temporarily unavailable", "Your pack ended without a confirmed renewal, so merchant features and the customer storefront are temporarily paused. When you are ready, you can choose the pack that suits you to restore access."),
            "action": "View available packs", "pack": "Pack",
        },
        "fr": {
            "seven_days": ("Votre forfait expire dans 7 jours", "Un rappel amical pour votre renouvellement", "Nous souhaitons vous rappeler que votre forfait actuel expire dans sept jours. Si vous souhaitez garder votre tableau de bord et votre boutique accessibles sans interruption, vous pouvez le renouveler au moment qui vous convient."),
            "three_days": ("Votre forfait expire dans 3 jours", "Un rappel concernant votre forfait", "Votre forfait actuel expire dans trois jours. Pour garder vos fonctions marchandes et votre boutique publique accessibles sans interruption, vous pouvez effectuer le renouvellement au moment qui vous convient."),
            "expired": ("Votre forfait a expiré", "Votre boutique est temporairement indisponible", "Votre forfait a pris fin sans renouvellement confirmé. Les fonctions marchandes et la boutique publique sont donc temporairement suspendues. Lorsque vous le souhaitez, vous pouvez choisir le forfait qui vous convient afin de rétablir l’accès."),
            "action": "Voir les forfaits disponibles", "pack": "Forfait",
        },
        "ar": {
            "seven_days": ("ستنتهي باقتك خلال 7 أيام", "تذكير ودي بموعد التجديد", "نود تذكيرك بأن باقتك الحالية ستنتهي خلال سبعة أيام. إذا رغبت في استمرار لوحة التحكم ومتجرك دون انقطاع، يمكنك تجديد الباقة في الوقت الذي يناسبك."),
            "three_days": ("ستنتهي باقتك خلال 3 أيام", "تذكير بموعد انتهاء الباقة", "ستنتهي باقتك الحالية خلال ثلاثة أيام. للمحافظة على ميزات التاجر والمتجر العام دون انقطاع، يمكنك إتمام التجديد في الوقت الذي يناسبك."),
            "expired": ("انتهت صلاحية باقتك", "متجرك غير متاح مؤقتًا", "انتهت باقتك دون تأكيد التجديد، ولذلك تم إيقاف ميزات التاجر والمتجر العام مؤقتًا. عندما يناسبك، يمكنك اختيار الباقة الملائمة لاستعادة الوصول."),
            "action": "عرض الباقات المتاحة", "pack": "الباقة",
        },
    }
    copy = copies.get(language, copies["en"])
    subject, title, paragraph = copy[event]
    return send_platform_email(
        to=user.email,
        subject=subject,
        preheader=paragraph,
        eyebrow="TijaraGO",
        title=title,
        recipient_name=user.first_name.strip() if user.first_name else "",
        paragraphs=[paragraph],
        details=[{"label": copy["pack"], "value": plan_name}],
        action_url=dashboard_url,
        action_label=copy["action"],
        language=language,
        logo_url=settings.MERCHANT_EMAIL_LOGO_URL,
        website_url=settings.MERCHANT_DASHBOARD_URL,
        headers={"X-TijaraGO-Event": f"subscription.{event}"},
    )


def send_merchant_verification(user, verification_url):
    """Showroom-branded first step: prove ownership of the signup address."""
    name = user.first_name.strip() if user.first_name else ""
    profile = user.merchant_profile
    expiry_minutes = max(1, math.ceil(settings.EMAIL_VERIFICATION_TIMEOUT_SECONDS / 60))
    # Unicode isolation keeps Latin brand/business names from reordering Arabic
    # punctuation in clients that only partially support CSS bidi rules.
    arabic_brand = f"\u2066{settings.PLATFORM_NAME}\u2069"
    business_name = profile.business_name.strip()
    arabic_business = f"\u2068{business_name}\u2069"
    copies = {
        "en": {
            "subject": f"Confirm your email to continue with {settings.PLATFORM_NAME}",
            "preheader": "One secure step before entering your merchant space.",
            "eyebrow": "TijaraGO showroom",
            "title": "Confirm your place in the showroom",
            "paragraphs": [
                (
                    f"Your account for {business_name} has been created. "
                    if business_name else "Your merchant account has been created. "
                ) + "Confirm your email address to complete the secure registration step.",
                "Once confirmed, we will let you know that your merchant space is ready and guide you toward creating your first storefront.",
            ],
            "details": [
                {"label": "Next step", "value": "Confirm your address and enter your merchant space"},
            ],
            "action": "Confirm my email",
            "note": f"This secure link expires in {expiry_minutes} minutes. Your phone number has been saved, but phone verification is not required yet.",
        },
        "fr": {
            "subject": f"Confirmez votre e-mail pour continuer avec {settings.PLATFORM_NAME}",
            "preheader": "Une étape sécurisée avant d’accéder à votre espace marchand.",
            "eyebrow": "Showroom TijaraGO",
            "title": "Confirmez votre accès au showroom",
            "paragraphs": [
                (
                    f"Votre compte pour {business_name} a été créé. "
                    if business_name else "Votre compte marchand a été créé. "
                ) + "Confirmez votre adresse e-mail pour terminer l’inscription sécurisée.",
                "Après confirmation, nous vous informerons que votre espace marchand est prêt et vous guiderons vers la création de votre première boutique.",
            ],
            "details": [
                {"label": "Prochaine étape", "value": "Confirmez votre adresse et ouvrez votre espace marchand"},
            ],
            "action": "Confirmer mon e-mail",
            "note": f"Ce lien sécurisé expire dans {expiry_minutes} minutes. Votre numéro est enregistré, mais sa vérification n’est pas encore requise.",
        },
        "ar": {
            "subject": f"أكد بريدك الإلكتروني للمتابعة مع {arabic_brand}",
            "preheader": "خطوة آمنة واحدة قبل الدخول إلى مساحة التاجر.",
            "eyebrow": f"معرض {arabic_brand}",
            "title": "أكد انضمامك إلى المعرض",
            "paragraphs": [
                (
                    f"تم إنشاء حساب {arabic_business}. "
                    if business_name else "تم إنشاء حساب التاجر الخاص بك. "
                ) + "أكد بريدك الإلكتروني لإكمال التسجيل الآمن.",
                "بعد التأكيد، سنخبرك بأن مساحة التاجر أصبحت جاهزة ونرشدك إلى إنشاء متجرك الأول.",
            ],
            "details": [
                {"label": "الخطوة التالية", "value": "أكد عنوانك ثم ادخل إلى مساحة التاجر"},
            ],
            "action": "تأكيد بريدي الإلكتروني",
            "note": f"تنتهي صلاحية هذا الرابط الآمن خلال {expiry_minutes} دقائق. تم حفظ رقم هاتفك، ولا يلزم التحقق منه حاليًا.",
        },
    }
    copy = copies.get(profile.preferred_language, copies["en"])
    return send_platform_email(
        to=user.email,
        subject=copy["subject"],
        preheader=copy["preheader"],
        eyebrow=copy["eyebrow"],
        title=copy["title"],
        recipient_name=name,
        paragraphs=copy["paragraphs"],
        details=copy["details"],
        action_url=verification_url,
        action_label=copy["action"],
        note=copy["note"],
        language=profile.preferred_language,
        brand_color=settings.SHOWROOM_EMAIL_BRAND_COLOR,
        brand_accent=settings.SHOWROOM_EMAIL_BRAND_ACCENT_COLOR,
        brand_ink=settings.SHOWROOM_EMAIL_BRAND_INK_COLOR,
        logo_url=settings.SHOWROOM_EMAIL_LOGO_URL,
        website_url=settings.SHOWROOM_URL,
        headers={"X-TijaraGO-Event": "merchant.email_verification"},
    )


def send_merchant_space_ready(user, dashboard_url=None):
    """Merchant-dashboard-branded onboarding, emitted once after verification."""
    profile = user.merchant_profile
    name = user.first_name.strip() if user.first_name else ""
    arabic_brand = f"\u2066{settings.PLATFORM_NAME}\u2069"
    business_name = profile.business_name.strip()
    arabic_business = f"\u2068{business_name}\u2069"
    copies = {
        "en": {
            "subject": f"Your {settings.PLATFORM_NAME} merchant space is ready",
            "preheader": "Your email is confirmed. Your business workspace is ready to open.",
            "eyebrow": "Merchant workspace",
            "title": "Everything is ready for your first store",
            "paragraphs": [
                (
                    f"Your email has been confirmed and the merchant space for {business_name} is ready."
                    if business_name else "Your email has been confirmed and your merchant space is ready."
                ),
                "Open your dashboard to create your storefront, organize products, and follow orders and customers from one place.",
            ],
            "details": [
                {"label": "Storefront", "value": "Choose a design and shape your brand"},
                {"label": "Catalog", "value": "Manage products, options, prices, and stock"},
                {"label": "Operations", "value": "Follow orders, customers, and performance"},
            ],
            "action": "Open my merchant space",
            "note": "Sign in with the email address and password you used during registration.",
        },
        "fr": {
            "subject": f"Votre espace marchand {settings.PLATFORM_NAME} est prêt",
            "preheader": "Votre e-mail est confirmé. Votre espace professionnel est prêt.",
            "eyebrow": "Espace marchand",
            "title": "Tout est prêt pour votre première boutique",
            "paragraphs": [
                (
                    f"Votre e-mail est confirmé et l’espace marchand de {business_name} est prêt."
                    if business_name else "Votre e-mail est confirmé et votre espace marchand est prêt."
                ),
                "Ouvrez votre tableau de bord pour créer votre vitrine, organiser vos produits et suivre commandes et clients depuis un seul espace.",
            ],
            "details": [
                {"label": "Vitrine", "value": "Choisissez un design et exprimez votre marque"},
                {"label": "Catalogue", "value": "Gérez produits, options, prix et stock"},
                {"label": "Activité", "value": "Suivez commandes, clients et performances"},
            ],
            "action": "Ouvrir mon espace marchand",
            "note": "Connectez-vous avec l’adresse e-mail et le mot de passe utilisés lors de l’inscription.",
        },
        "ar": {
            "subject": f"مساحة التاجر في {arabic_brand} جاهزة",
            "preheader": "تم تأكيد بريدك الإلكتروني وأصبحت مساحة نشاطك جاهزة.",
            "eyebrow": "مساحة التاجر",
            "title": "كل شيء جاهز لإنشاء متجرك الأول",
            "paragraphs": [
                (
                    f"تم تأكيد بريدك الإلكتروني وأصبحت مساحة التاجر الخاصة بـ {arabic_business} جاهزة."
                    if business_name else "تم تأكيد بريدك الإلكتروني وأصبحت مساحة التاجر الخاصة بك جاهزة."
                ),
                "افتح لوحة التحكم لإنشاء واجهة متجرك وتنظيم المنتجات ومتابعة الطلبات والعملاء من مكان واحد.",
            ],
            "details": [
                {"label": "واجهة المتجر", "value": "اختر تصميمًا وعبّر عن علامتك"},
                {"label": "الكتالوج", "value": "أدر المنتجات والخيارات والأسعار والمخزون"},
                {"label": "العمليات", "value": "تابع الطلبات والعملاء والأداء"},
            ],
            "action": "فتح مساحة التاجر",
            "note": "سجل الدخول بالبريد الإلكتروني وكلمة المرور المستخدمين أثناء التسجيل.",
        },
    }
    copy = copies.get(profile.preferred_language, copies["en"])
    dashboard_url = (dashboard_url or settings.MERCHANT_DASHBOARD_URL).rstrip("/")
    return send_platform_email(
        to=user.email,
        subject=copy["subject"],
        preheader=copy["preheader"],
        eyebrow=copy["eyebrow"],
        title=copy["title"],
        recipient_name=name,
        paragraphs=copy["paragraphs"],
        details=copy["details"],
        action_url=dashboard_url,
        action_label=copy["action"],
        note=copy["note"],
        language=profile.preferred_language,
        brand_color=settings.MERCHANT_EMAIL_BRAND_COLOR,
        brand_accent=settings.MERCHANT_EMAIL_BRAND_ACCENT_COLOR,
        brand_ink=settings.MERCHANT_EMAIL_BRAND_INK_COLOR,
        logo_url=settings.MERCHANT_EMAIL_LOGO_URL,
        website_url=dashboard_url,
        headers={"X-TijaraGO-Event": "merchant.space_ready"},
    )


def send_customer_verification(user, store, verification_url):
    """Send a store-branded address confirmation for a customer account."""
    profile = user.client_profile
    language = profile.preferred_language
    expiry_minutes = max(1, math.ceil(settings.EMAIL_VERIFICATION_TIMEOUT_SECONDS / 60))
    copies = {
        "en": {
            "subject": f"Confirm your email for {store.name}",
            "preheader": "Confirm your address to securely access your orders and profile.",
            "eyebrow": "Customer account",
            "title": f"Welcome to {store.name}",
            "paragraphs": [
                "Confirm your email address to activate your customer account.",
                "Once confirmed, you can follow your orders and keep your delivery details up to date from one private space.",
            ],
            "details": [
                {"label": "Orders", "value": "Follow status and delivery updates"},
                {"label": "Profile", "value": "Keep your contact and delivery details ready"},
            ],
            "action": "Confirm my email",
            "note": f"This secure link expires in {expiry_minutes} minutes. Your phone number is saved for order contact and is not SMS-verified yet.",
        },
        "fr": {
            "subject": f"Confirmez votre e-mail pour {store.name}",
            "preheader": "Confirmez votre adresse pour accéder à vos commandes et à votre profil.",
            "eyebrow": "Compte client",
            "title": f"Bienvenue chez {store.name}",
            "paragraphs": [
                "Confirmez votre adresse e-mail pour activer votre compte client.",
                "Vous pourrez ensuite suivre vos commandes et garder vos coordonnées de livraison à jour dans un espace privé.",
            ],
            "details": [
                {"label": "Commandes", "value": "Suivez leur statut et leur livraison"},
                {"label": "Profil", "value": "Gardez vos coordonnées prêtes pour vos achats"},
            ],
            "action": "Confirmer mon e-mail",
            "note": f"Ce lien sécurisé expire dans {expiry_minutes} minutes. Votre numéro est enregistré pour le suivi des commandes, sans vérification SMS pour le moment.",
        },
        "ar": {
            "subject": f"أكد بريدك الإلكتروني لدى \u2068{store.name}\u2069",
            "preheader": "أكد عنوانك للوصول بأمان إلى طلباتك وملفك الشخصي.",
            "eyebrow": "حساب العميل",
            "title": f"مرحبًا بك لدى \u2068{store.name}\u2069",
            "paragraphs": [
                "أكد بريدك الإلكتروني لتفعيل حساب العميل.",
                "بعد التأكيد، يمكنك متابعة طلباتك وتحديث بيانات التواصل والتوصيل من مساحة خاصة واحدة.",
            ],
            "details": [
                {"label": "الطلبات", "value": "تابع الحالة وتحديثات التوصيل"},
                {"label": "الملف الشخصي", "value": "احتفظ ببيانات التواصل والتوصيل جاهزة"},
            ],
            "action": "تأكيد بريدي الإلكتروني",
            "note": f"تنتهي صلاحية هذا الرابط الآمن خلال {expiry_minutes} دقائق. رقم الهاتف محفوظ للتواصل بشأن الطلب، ولا يخضع حاليًا للتحقق عبر الرسائل النصية.",
        },
    }
    copy = copies.get(language, copies["en"])
    logo_url = settings.PLATFORM_EMAIL_LOGO_URL
    if store.logo:
        logo_url = media_asset_url(store.logo, base_url=settings.STOREFRONT_API_URL) or logo_url
    return send_platform_email(
        to=user.email,
        subject=copy["subject"],
        preheader=copy["preheader"],
        eyebrow=copy["eyebrow"],
        title=copy["title"],
        recipient_name=user.first_name.strip() if user.first_name else "",
        paragraphs=copy["paragraphs"],
        details=copy["details"],
        action_url=verification_url,
        action_label=copy["action"],
        note=copy["note"],
        language=language,
        logo_url=logo_url,
        brand_name=store.name,
        headers={"X-TijaraGO-Event": "customer.email_verification"},
    )


def send_subscription_activated(payment):
    user = payment.user
    scope = "All your stores" if payment.entitlement_scope == "all_owned_stores" else (
        payment.selected_store.name if payment.selected_store_id else "Selected store"
    )
    return send_platform_email(
        to=user.email,
        subject=f"Your {payment.plan_name} membership is active",
        preheader="Your new plan benefits are now available.",
        eyebrow="Membership confirmed",
        title="Your plan is now active",
        recipient_name=user.first_name.strip() if user.first_name else "",
        paragraphs=[
            "Your payment was confirmed securely and your membership benefits are ready to use.",
        ],
        details=[
            {"label": "Plan", "value": payment.plan_name},
            {"label": "Coverage", "value": scope},
            {"label": "Amount", "value": f"{payment.amount} {payment.currency}"},
            {"label": "Access period", "value": f"{payment.duration_days} days"},
        ],
        action_url=settings.MERCHANT_DASHBOARD_URL,
        action_label="Open my dashboard",
        note="Keep this message as confirmation of your membership purchase.",
        headers={
            "X-TijaraGO-Event": "membership.activated",
            "X-Entity-Ref-ID": str(payment.pk),
        },
    )


def send_subscription_payment_failed(payment):
    return send_platform_email(
        to=payment.user.email,
        subject="We could not confirm your membership payment",
        preheader="Your current access has not been changed.",
        eyebrow="Payment update",
        title="Your membership was not changed",
        recipient_name=payment.user.first_name.strip() if payment.user.first_name else "",
        paragraphs=[
            "We could not confirm the payment for the selected membership. No new plan was activated.",
            "You can safely return to your dashboard and try again when you are ready.",
        ],
        details=[{"label": "Selected plan", "value": payment.plan_name}],
        action_url=f"{settings.MERCHANT_DASHBOARD_URL}/subscription",
        action_label="Review membership options",
        note="For your security, this email never includes payment credentials or full card details.",
        headers={
            "X-TijaraGO-Event": "membership.payment_failed",
            "X-Entity-Ref-ID": str(payment.pk),
        },
    )


def send_customer_password_reset(user, store, reset_url):
    """Send a neutral, store-branded recovery link without disclosing account data."""
    language = user.client_profile.preferred_language
    copies = {
        "en": ("Reset your password", "Use this secure link to choose a new password.", "Reset password", "If you did not request this, you can ignore this email."),
        "fr": ("Réinitialisez votre mot de passe", "Utilisez ce lien sécurisé pour choisir un nouveau mot de passe.", "Réinitialiser le mot de passe", "Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail."),
        "ar": ("إعادة تعيين كلمة المرور", "استخدم هذا الرابط الآمن لاختيار كلمة مرور جديدة.", "إعادة تعيين كلمة المرور", "إذا لم تطلب ذلك، يمكنك تجاهل هذه الرسالة."),
    }
    title, body, action, note = copies.get(language, copies["en"])
    minutes = max(1, math.ceil(settings.PASSWORD_RESET_TIMEOUT_SECONDS / 60))
    expiry = {"en": f"This link expires in {minutes} minutes. ", "fr": f"Ce lien expire dans {minutes} minutes. ", "ar": f"تنتهي صلاحية الرابط خلال {minutes} دقيقة. "}
    note = expiry.get(language, expiry["en"]) + note
    logo_url = settings.PLATFORM_EMAIL_LOGO_URL
    if store.logo:
        logo_url = media_asset_url(store.logo, base_url=settings.STOREFRONT_API_URL) or logo_url
    return send_platform_email(
        to=user.email,
        subject=f"{title} — {store.name}",
        preheader=body,
        eyebrow=store.name,
        title=title,
        recipient_name=user.first_name.strip() if user.first_name else "",
        paragraphs=[body],
        action_url=reset_url,
        action_label=action,
        note=note,
        language=language,
        logo_url=logo_url,
        brand_name=store.name,
        headers={"X-TijaraGO-Event": "customer.password_reset"},
    )



def send_merchant_password_reset(user, reset_url):
    """Use the existing transactional email backend for merchant recovery."""
    language = user.merchant_profile.preferred_language if hasattr(user, "merchant_profile") else "ar"
    copies = {
        "en": ("Reset your TijaraGO password", "Use this secure link to choose a new password.", "Reset password", "If you did not request it, you can ignore this email."),
        "fr": ("Réinitialisez votre mot de passe TijaraGO", "Utilisez ce lien sécurisé pour choisir un nouveau mot de passe.", "Réinitialiser le mot de passe", "Si vous n'avez rien demandé, ignorez cet e-mail."),
        "ar": ("إعادة تعيين كلمة مرور TijaraGO", "استخدم هذا الرابط الآمن لاختيار كلمة مرور جديدة.", "إعادة تعيين كلمة المرور", "إذا لم تطلب ذلك، تجاهل هذه الرسالة."),
    }
    title, body, action, note = copies.get(language, copies["en"])
    minutes = max(1, math.ceil(settings.PASSWORD_RESET_TIMEOUT_SECONDS / 60))
    expiry = {"en": f"This link expires in {minutes} minutes. ", "fr": f"Ce lien expire dans {minutes} minutes. ", "ar": f"تنتهي صلاحية الرابط خلال {minutes} دقيقة. "}
    note = expiry.get(language, expiry["en"]) + note
    return send_platform_email(
        to=user.email, subject=title, preheader=body, eyebrow="TijaraGO",
        title=title, recipient_name=user.first_name.strip() if user.first_name else "",
        paragraphs=[body], action_url=reset_url, action_label=action,
        note=note, language=language,
        headers={"X-TijaraGO-Event": "merchant.password_reset"},
    )
