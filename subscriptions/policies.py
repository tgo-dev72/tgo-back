"""Governed, reusable business rules for subscription entitlements.

Plans only provide values. Application behavior is registered here once and
can then be reused by any number of plans without plan-specific code.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from store.models import StoreMember


STORE_LIMIT_KEY = "stores.max_count"
PREMIUM_DESIGNS_KEY = "designs.premium"
TEAM_LIMIT_KEY = "team.max_members"
PRODUCT_LIMIT_KEY = "catalog.max_products"
CUSTOM_DOMAIN_KEY = "domains.custom"
REMOVE_BRANDING_KEY = "branding.remove_platform"
INVOICING_KEY = "invoicing.issue"
ANALYTICS_LEVEL_KEY = "analytics.level"
EMAIL_SEND_LIMIT_KEY = "marketing.email_monthly_sends"
MARKETING_AUTOMATION_KEY = "marketing.automation"
SCHEDULED_PROMOTIONS_KEY = "promotions.scheduled"
CATALOG_IMPORT_KEY = "catalog.import"
LEGACY_IMPORT_EXPORT_KEY = "catalog.import_export"
INVENTORY_LOCATION_LIMIT_KEY = "inventory.max_locations"
AUDIT_LOG_KEY = "operations.audit_log"
API_ACCESS_KEY = "integrations.api_access"
WEBHOOKS_KEY = "integrations.webhooks"
SUPPORT_LEVEL_KEY = "support.level"


@dataclass(frozen=True)
class BusinessRuleTemplate:
    key: str
    name: str
    description: str
    value_type: str
    scope: str
    unit: str
    kind: str
    default_value: object
    allowed_values: tuple = ()
    sort_order: int = 10
    operation: str | None = None
    usage_counter: Callable | None = None
    required: bool = True
    active: bool = True
    translations: dict | None = None
    zero_means_unlimited: bool = False
    counting_rules: str = ""
    reset_window: str = "none"
    missing_behavior: str = "deny"
    zero_behavior: str = "not applicable"
    downgrade_behavior: str = "Existing data is retained; new restricted actions are denied."

    def as_dict(self, configured=False):
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "value_type": self.value_type,
            "scope": self.scope,
            "unit": self.unit,
            "kind": self.kind,
            "default_value": self.default_value,
            "allowed_values": list(self.allowed_values),
            "sort_order": self.sort_order,
            "operation": self.operation,
            "configured": configured,
            "required": self.required,
            "active": self.active,
            "translations": self.translations or {},
            "zero_means_unlimited": self.zero_means_unlimited,
            "counting_rules": self.counting_rules,
            "reset_window": self.reset_window,
            "missing_behavior": self.missing_behavior,
            "zero_behavior": self.zero_behavior,
            "downgrade_behavior": self.downgrade_behavior,
        }


def _owned_store_count(user, _context=None):
    return StoreMember.objects.filter(
        user=user,
        role=StoreMember.Roles.OWNER,
    ).count()


def _owned_store_ids(user):
    return StoreMember.objects.filter(
        user=user,
        role=StoreMember.Roles.OWNER,
    ).values_list("store_id", flat=True)


def _team_member_count(user, _context=None):
    from django.utils import timezone
    from access_control.models import StaffInvitation

    store_ids = list(_owned_store_ids(user))
    member_emails = {
        email.lower()
        for email in StoreMember.objects.filter(store_id__in=store_ids)
        .values_list("user__email", flat=True)
        if email
    }
    # A live invitation reserves a seat. Otherwise an owner could create many
    # invitations while under quota and exceed the plan as they are accepted.
    pending_emails = {
        email.lower()
        for email in StaffInvitation.objects.filter(
            store_id__in=store_ids,
            status=StaffInvitation.Status.PENDING,
            expires_at__gt=timezone.now(),
        ).values_list("email", flat=True)
        if email
    }
    return len(member_emails | pending_emails)


def _product_count(_user, context=None):
    store = (context or {}).get("store")
    return store.products.count() if store else 0


def _monthly_email_send_count(_user, context=None):
    from django.db.models import Sum
    from django.utils import timezone

    store = (context or {}).get("store")
    if not store:
        return 0
    since = timezone.now() - timedelta(days=30)
    sent = store.campaigns.filter(sent_at__gte=since).aggregate(total=Sum("sent_count"))["total"] or 0
    # A send is reserved before the email loop starts. Count recent in-flight
    # reservations so concurrent requests cannot both pass the same quota.
    # Older reservations are recovered by CampaignSendView before evaluation.
    reserved = store.campaigns.filter(
        status="sending",
        updated_at__gte=timezone.now() - timedelta(hours=1),
    ).aggregate(total=Sum("recipients_count"))["total"] or 0
    return sent + reserved


def _copy(en_name, en_description, fr_name, fr_description, ar_name, ar_description):
    return {
        "en": {"name": en_name, "description": en_description},
        "fr": {"name": fr_name, "description": fr_description},
        "ar": {"name": ar_name, "description": ar_description},
    }


BUSINESS_RULES = {
    STORE_LIMIT_KEY: BusinessRuleTemplate(
        key=STORE_LIMIT_KEY,
        name="Stores included",
        description="The number of online stores a merchant can own at the same time.",
        value_type="integer",
        scope="account",
        unit="stores",
        kind="resource_limit",
        default_value=1,
        sort_order=5,
        operation="stores.create",
        usage_counter=_owned_store_count,
        counting_rules="Owned stores across the merchant account count, regardless of storefront publication state. Staff memberships do not count.",
        zero_behavior="Zero allows no owned stores.",
        translations=_copy(
            "Stores included", "Maximum number of online stores owned by the merchant.",
            "Boutiques incluses", "Nombre maximal de boutiques en ligne appartenant au marchand.",
            "المتاجر المشمولة", "العدد الأقصى للمتاجر الإلكترونية التي يملكها التاجر.",
        ),
    ),
    PREMIUM_DESIGNS_KEY: BusinessRuleTemplate(
        key=PREMIUM_DESIGNS_KEY,
        name="Premium storefront collection",
        description="Lets merchants choose from the premium storefront design collection.",
        value_type="boolean",
        scope="store",
        unit="",
        kind="feature_access",
        default_value=False,
        sort_order=10,
        operation="designs.select",
        counting_rules="Boolean access is resolved from subscriptions that cover the selected store.",
        zero_behavior="False denies new premium design selections.",
        downgrade_behavior="The existing design is retained; selecting another premium design is denied.",
        translations=_copy(
            "Premium designs", "Access to the complete premium storefront design collection.",
            "Designs Premium", "Accès à toute la collection de designs de boutique Premium.",
            "التصاميم المميزة", "الوصول إلى المجموعة الكاملة من تصاميم المتاجر المميزة.",
        ),
    ),
    TEAM_LIMIT_KEY: BusinessRuleTemplate(
        key=TEAM_LIMIT_KEY, name="Merchant team members", description="People who can help manage the merchant's stores.",
        value_type="integer", scope="account", unit="members", kind="resource_limit", default_value=1,
        sort_order=15, operation="team.invite", usage_counter=_team_member_count,
        counting_rules="Distinct member email addresses across all stores owned by the account, including the owner and unexpired pending invitations. Duplicate invitations or memberships for the same email reserve one seat.",
        zero_behavior="Zero allows no team seats.",
        downgrade_behavior="Existing members remain; new members and over-limit invitation acceptance are denied.",
        translations=_copy(
            "Merchant team members", "Total people, including the owner, who can manage the merchant's stores.",
            "Membres de l’équipe", "Nombre total de personnes, propriétaire inclus, pouvant gérer les boutiques.",
            "أعضاء فريق التاجر", "إجمالي الأشخاص، بمن فيهم المالك، الذين يمكنهم إدارة المتاجر.",
        ),
    ),
    PRODUCT_LIMIT_KEY: BusinessRuleTemplate(
        key=PRODUCT_LIMIT_KEY, name="Products per store", description="Maximum products in each store.",
        value_type="integer", scope="store", unit="products", kind="resource_limit", default_value=100,
        sort_order=20, operation="catalog.products.create", usage_counter=_product_count, zero_means_unlimited=True,
        counting_rules="Products per store. Active and inactive products count once; variants do not count as separate products.",
        zero_behavior="Zero means unlimited products.",
        downgrade_behavior="Existing products remain; new products and imports that create products are denied above the limit.",
        translations=_copy(
            "Products per store", "Maximum number of products that can be created in each store; zero means unlimited.",
            "Produits par boutique", "Nombre maximal de produits par boutique ; zéro signifie illimité.",
            "المنتجات لكل متجر", "الحد الأقصى للمنتجات في كل متجر؛ الصفر يعني غير محدود.",
        ),
    ),
    CUSTOM_DOMAIN_KEY: BusinessRuleTemplate(
        key=CUSTOM_DOMAIN_KEY, name="Custom domain", description="Connect a domain owned by the merchant.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False,
        sort_order=25, operation="domains.connect",
        counting_rules="Boolean access is resolved from subscriptions that cover the selected store; DNS ownership and verification remain mandatory.",
        zero_behavior="False denies connecting a new custom domain.",
        downgrade_behavior="Existing domain records are retained; connecting another domain is denied.",
        translations=_copy(
            "Custom domain", "Connect a professional domain owned by the merchant.",
            "Domaine personnalisé", "Connectez un domaine professionnel appartenant au marchand.",
            "نطاق مخصص", "ربط نطاق احترافي يملكه التاجر.",
        ),
    ),
    REMOVE_BRANDING_KEY: BusinessRuleTemplate(
        key=REMOVE_BRANDING_KEY, name="Remove platform branding", description="Use only the merchant's identity on the storefront.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False,
        sort_order=30,
        counting_rules="Boolean access is resolved from subscriptions that cover the selected store and combined with the merchant's saved branding preference.",
        zero_behavior="False displays TijaraGO attribution.",
        downgrade_behavior="The preference is retained, but TijaraGO attribution becomes effective immediately.",
        translations=_copy(
            "Merchant-only branding", "Remove TijaraGO attribution from the customer-facing storefront.",
            "Identité exclusivement marchande", "Retirez la mention TijaraGO de la boutique visible par les clients.",
            "هوية التاجر فقط", "إزالة الإشارة إلى TijaraGO من واجهة المتجر الموجهة للعملاء.",
        ),
    ),
    INVOICING_KEY: BusinessRuleTemplate(
        key=INVOICING_KEY,
        name="Invoice issuance",
        description="Create, render, and deliver compliant customer invoices.",
        value_type="boolean",
        scope="store",
        unit="",
        kind="feature_access",
        default_value=False,
        sort_order=32,
        operation="invoicing.issue",
        counting_rules="Boolean access is resolved from the owner subscription covering the selected store.",
        zero_behavior="False denies invoice activation, preview, and new invoice issuance.",
        downgrade_behavior="Issued invoices remain available; new invoice issuance is denied.",
        translations=_copy(
            "Invoice issuance", "Create, render, and deliver customer invoices.",
            "Émission de factures", "Créez, générez et envoyez des factures aux clients.",
            "إصدار الفواتير", "إنشاء فواتير العملاء وتجهيزها وإرسالها.",
        ),
    ),
    ANALYTICS_LEVEL_KEY: BusinessRuleTemplate(
        key=ANALYTICS_LEVEL_KEY, name="Analytics", description="Essential covers 30-day commerce health; Standard adds a 90-day conversion funnel, product performance and search demand; Advanced adds annual comparisons, source attribution and opportunity alerts.",
        value_type="choice", scope="account", unit="", kind="service_level", default_value="essential",
        allowed_values=("essential", "standard", "advanced"), sort_order=35,
        counting_rules="Account-level access covers stores included by the subscription. Essential unlocks 30-day commerce health; Standard adds 90-day funnel, product and search reports; Advanced adds annual ranges, attribution and opportunity reports.",
        missing_behavior="Use the Essential level.",
        zero_behavior="Not applicable; levels are ordered Essential, Standard, Advanced.",
        downgrade_behavior="Historical commerce data remains; reports and date ranges above the current level are hidden.",
        translations=_copy(
            "Analytics", "Essential covers 30-day commerce health; Standard adds a 90-day conversion funnel, product performance and search demand; Advanced adds annual comparisons, source attribution and opportunity alerts.",
            "Analyses", "Essentiel couvre 30 jours; Standard ajoute l'entonnoir sur 90 jours, la performance produit et la demande; Avancé ajoute comparaisons annuelles, attribution et alertes.",
            "التحليلات", "الأساسي يغطي صحة التجارة خلال 30 يومًا؛ القياسي يضيف مسار تحويل 90 يومًا وأداء المنتجات والبحث؛ والمتقدم يضيف المقارنات السنوية ومصادر الزيارات والتنبيهات.",
        ),
    ),
    EMAIL_SEND_LIMIT_KEY: BusinessRuleTemplate(
        key=EMAIL_SEND_LIMIT_KEY, name="Monthly marketing emails", description="Marketing email deliveries available every 30 days.",
        value_type="integer", scope="store", unit="emails", kind="resource_limit", default_value=100,
        sort_order=40, operation="marketing.campaign.send", usage_counter=_monthly_email_send_count,
        counting_rules="Successful campaign deliveries plus recent in-flight recipient reservations, per store. Transactional order and platform emails do not count.",
        reset_window="rolling 30 days",
        zero_behavior="Zero allows no marketing email deliveries.",
        downgrade_behavior="Campaign history remains; new sends are denied when rolling usage exceeds the new limit.",
        translations=_copy(
            "Monthly marketing emails", "Number of campaign emails available to each store every 30 days.",
            "E-mails marketing mensuels", "Nombre d’e-mails de campagne disponibles par boutique tous les 30 jours.",
            "رسائل التسويق الشهرية", "عدد رسائل الحملات المتاحة لكل متجر خلال كل 30 يومًا.",
        ),
    ),
    MARKETING_AUTOMATION_KEY: BusinessRuleTemplate(
        key=MARKETING_AUTOMATION_KEY, name="Marketing automation", description="Automated customer follow-up campaigns.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False, sort_order=45,
        translations=_copy(
            "Marketing automation", "Automated customer follow-ups based on store activity.",
            "Automatisation marketing", "Relances clients automatisées selon l’activité de la boutique.",
            "أتمتة التسويق", "متابعات آلية للعملاء بناءً على نشاط المتجر.",
        ),
    ),
    SCHEDULED_PROMOTIONS_KEY: BusinessRuleTemplate(
        key=SCHEDULED_PROMOTIONS_KEY, name="Scheduled promotions", description="Plan promotions with automatic start and end dates.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False, sort_order=50,
        translations=_copy(
            "Scheduled promotions", "Plan promotions that start and end automatically.",
            "Promotions programmées", "Planifiez des promotions avec début et fin automatiques.",
            "العروض المجدولة", "جدولة عروض تبدأ وتنتهي تلقائيًا.",
        ),
    ),
    CATALOG_IMPORT_KEY: BusinessRuleTemplate(
        key=CATALOG_IMPORT_KEY, name="Catalog import", description="Import product information using structured files.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False, sort_order=55,
        operation="catalog.import",
        counting_rules="Boolean permission is resolved from the owner subscription covering the selected store. Store authorization and products.create permission are also required.",
        zero_behavior="False denies import validation and commit. Basic export remains available with products.view permission.",
        downgrade_behavior="Completed imports and products remain; new import validation and commit are denied.",
        translations=_copy(
            "Catalog import", "Import product information using CSV or Excel files.",
            "Import du catalogue", "Importez les produits avec des fichiers CSV ou Excel.",
            "استيراد الكتالوج", "استيراد بيانات المنتجات باستخدام ملفات CSV أو Excel.",
        ),
    ),
    LEGACY_IMPORT_EXPORT_KEY: BusinessRuleTemplate(
        key=LEGACY_IMPORT_EXPORT_KEY,
        name="Legacy catalog import and export",
        description="Deprecated compatibility value copied to catalog.import during migration; it grants no runtime access.",
        value_type="boolean", scope="store", unit="", kind="feature_access", default_value=False,
        sort_order=56, required=False,
        active=False,
        counting_rules="Deprecated metadata only. catalog.import is authoritative; export is permission-only.",
        missing_behavior="No effect.",
        zero_behavior="No effect.",
        downgrade_behavior="No effect. Remove after all clients and stored revisions use catalog.import.",
        translations=_copy(
            "Legacy catalog transfer", "Deprecated compatibility value; use Catalog import.",
            "Transfert catalogue historique", "Valeur de compatibilité obsolète ; utilisez Import du catalogue.",
            "نقل الكتالوج القديم", "قيمة توافق قديمة؛ استخدم استيراد الكتالوج.",
        ),
    ),
    INVENTORY_LOCATION_LIMIT_KEY: BusinessRuleTemplate(
        key=INVENTORY_LOCATION_LIMIT_KEY, name="Inventory locations", description="Locations used to manage inventory.",
        value_type="integer", scope="store", unit="locations", kind="resource_limit", default_value=1, sort_order=60,
        translations=_copy(
            "Inventory locations", "Number of locations that can hold and manage stock.",
            "Emplacements de stock", "Nombre d’emplacements pouvant détenir et gérer le stock.",
            "مواقع المخزون", "عدد المواقع التي يمكنها الاحتفاظ بالمخزون وإدارته.",
        ),
    ),
    AUDIT_LOG_KEY: BusinessRuleTemplate(
        key=AUDIT_LOG_KEY, name="Operational audit log", description="History of important team and store changes.",
        value_type="boolean", scope="account", unit="", kind="feature_access", default_value=False, sort_order=65,
        translations=_copy(
            "Operational audit log", "Review important actions performed by the merchant team.",
            "Journal d’audit", "Consultez les actions importantes réalisées par l’équipe marchande.",
            "سجل العمليات", "مراجعة الإجراءات المهمة التي ينفذها فريق التاجر.",
        ),
    ),
    API_ACCESS_KEY: BusinessRuleTemplate(
        key=API_ACCESS_KEY, name="API access", description="Connect approved external business tools.",
        value_type="boolean", scope="account", unit="", kind="feature_access", default_value=False, sort_order=70,
        translations=_copy(
            "API access", "Connect approved external tools to merchant data and operations.",
            "Accès API", "Connectez des outils externes approuvés aux données et opérations marchandes.",
            "الوصول إلى API", "ربط أدوات خارجية معتمدة ببيانات وعمليات التاجر.",
        ),
    ),
    WEBHOOKS_KEY: BusinessRuleTemplate(
        key=WEBHOOKS_KEY, name="Webhooks", description="Send real-time events to approved integrations.",
        value_type="boolean", scope="account", unit="", kind="feature_access", default_value=False, sort_order=75,
        translations=_copy(
            "Webhooks", "Send real-time store events to approved integrations.",
            "Webhooks", "Envoyez les événements de boutique en temps réel aux intégrations approuvées.",
            "إشعارات Webhook", "إرسال أحداث المتجر لحظيًا إلى التكاملات المعتمدة.",
        ),
    ),
    SUPPORT_LEVEL_KEY: BusinessRuleTemplate(
        key=SUPPORT_LEVEL_KEY, name="Support level", description="Response and assistance level for the merchant.",
        value_type="choice", scope="account", unit="", kind="service_level", default_value="standard",
        allowed_values=("standard", "priority", "dedicated"), sort_order=80,
        translations=_copy(
            "Support level", "Level of assistance available to the merchant team.",
            "Niveau d’assistance", "Niveau d’accompagnement disponible pour l’équipe marchande.",
            "مستوى الدعم", "مستوى المساعدة المتاح لفريق التاجر.",
        ),
    ),
}


class EntitlementDenied(RuntimeError):
    def __init__(self, *, code, message, operation, rule, limit=None, used=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation = operation
        self.rule = rule
        self.limit = limit
        self.used = used

    def as_dict(self):
        payload = {
            "code": self.code,
            "detail": self.message,
            "operation": self.operation,
            "rule": self.rule,
        }
        if self.limit is not None:
            payload["limit"] = self.limit
        if self.used is not None:
            payload["used"] = self.used
        return payload


def get_business_rule(key):
    return BUSINESS_RULES.get(key)


def business_rule_catalog(configured_keys=()):
    configured = set(configured_keys)
    return [rule.as_dict(rule.key in configured) for rule in BUSINESS_RULES.values()]


def definition_contract_errors(*, key, value_type, scope):
    rule = get_business_rule(key)
    if rule is None:
        return {
            "key": "Choose a supported business rule. New product behavior must be registered before it can be offered.",
        }
    errors = {}
    if value_type != rule.value_type:
        errors["value_type"] = "This value format is fixed by the selected business rule."
    if scope != rule.scope:
        errors["scope"] = "This coverage level is fixed by the selected business rule."
    return errors


def enforce_operation(user, operation, *, context=None):
    """Evaluate every registered rule for an operation.

    The caller owns transaction boundaries. Resource-creation callers should
    lock the merchant user row before invoking this function, ensuring a count
    and subsequent insert cannot race another request for the same account.
    """
    from .entitlements import resolve_entitlement
    from .models import EntitlementDefinition

    rules = [rule for rule in BUSINESS_RULES.values() if rule.operation == operation]
    for rule in rules:
        definition = EntitlementDefinition.objects.filter(key=rule.key, is_active=True).first()
        if definition is None:
            # Required commercial protections fail closed during a bad or
            # partially deployed configuration.
            if rule.required:
                raise EntitlementDenied(
                    code="plan_rules_unavailable",
                    message="We could not confirm what your plan includes. Please try again shortly.",
                    operation=operation,
                    rule=rule.key,
                )
            continue

        value = resolve_entitlement(user, rule.key, context.get("store") if context else None)
        if rule.kind == "feature_access" and not bool(value):
            raise EntitlementDenied(
                code="feature_not_included",
                message="This option is not included in your current plan.",
                operation=operation,
                rule=rule.key,
            )
        if rule.kind == "resource_limit":
            limit = int(value)
            used = int(rule.usage_counter(user, context or {})) if rule.usage_counter else 0
            requested = int((context or {}).get("amount", 1))
            if rule.zero_means_unlimited and limit == 0:
                continue
            if used + requested > limit:
                label = definition.unit or rule.unit or "items"
                if limit == 1 and label == "stores":
                    label = "store"
                raise EntitlementDenied(
                    code="store_limit_reached" if rule.key == STORE_LIMIT_KEY else "plan_limit_reached",
                    message=f"Your current plan includes {limit} {label}. Choose a plan with more room to continue growing.",
                    operation=operation,
                    rule=rule.key,
                    limit=limit,
                    used=used,
                )
    return True
