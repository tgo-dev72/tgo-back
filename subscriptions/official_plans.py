"""Authoritative commercial definition for TijaraGO's public subscription packs."""

from decimal import Decimal


OFFICIAL_ENTITLEMENT_KEYS = (
    "stores.max_count",
    "designs.premium",
    "team.max_members",
    "catalog.max_products",
    "domains.custom",
    "branding.remove_platform",
    "invoicing.issue",
    "analytics.level",
    "marketing.email_monthly_sends",
    "catalog.import",
)

OFFICIAL_PLANS = {
    "free": {
        "price": Decimal("0.00"), "sort_order": 0, "featured": False,
        "copy": {
            "en": ("Free", "Start selling online without an initial subscription."),
            "fr": ("Gratuit", "Commencez à vendre en ligne sans abonnement initial."),
            "ar": ("مجاني", "بدا البيع أونلاين بلا اشتراك فالبداية."),
        },
        "entitlements": {
            "stores.max_count": 1, "designs.premium": False, "team.max_members": 1,
            "catalog.max_products": 50, "domains.custom": False,
            "branding.remove_platform": False, "invoicing.issue": False,
            "analytics.level": "essential", "marketing.email_monthly_sends": 0,
            "catalog.import": False,
        },
    },
    "starter": {
        "price": Decimal("79.00"), "sort_order": 10, "featured": False,
        "copy": {
            "en": ("Starter", "Essential tools for a professional online store."),
            "fr": ("Starter", "Les outils essentiels pour professionnaliser votre boutique."),
            "ar": ("Starter", "الأدوات الأساسية باش تطوّر متجرك."),
        },
        "entitlements": {
            "stores.max_count": 1, "designs.premium": False, "team.max_members": 2,
            "catalog.max_products": 500, "domains.custom": True,
            "branding.remove_platform": False, "invoicing.issue": True,
            "analytics.level": "standard", "marketing.email_monthly_sends": 200,
            "catalog.import": True,
        },
    },
    "growth": {
        "price": Decimal("149.00"), "sort_order": 20, "featured": True,
        "copy": {
            "en": ("Growth", "Grow your brand and business."),
            "fr": ("Growth", "Développez votre marque et votre activité."),
            "ar": ("Growth", "طوّر علامتك التجارية وكبّر نشاطك."),
        },
        "entitlements": {
            "stores.max_count": 3, "designs.premium": True, "team.max_members": 5,
            "catalog.max_products": 2000, "domains.custom": True,
            "branding.remove_platform": True, "invoicing.issue": True,
            "analytics.level": "advanced", "marketing.email_monthly_sends": 1000,
            "catalog.import": True,
        },
    },
    "business": {
        "price": Decimal("249.00"), "sort_order": 30, "featured": False,
        "copy": {
            "en": ("Business", "More capacity for your growing business."),
            "fr": ("Business", "Plus de capacité pour accompagner votre croissance."),
            "ar": ("Business", "إمكانيات أكبر باش تواكب نمو تجارتك."),
        },
        "entitlements": {
            "stores.max_count": 5, "designs.premium": True, "team.max_members": 10,
            "catalog.max_products": 10000, "domains.custom": True,
            "branding.remove_platform": True, "invoicing.issue": True,
            "analytics.level": "advanced", "marketing.email_monthly_sends": 3000,
            "catalog.import": True,
        },
    },
}


def plan_translations(config):
    return {
        language: {"name": name, "description": description}
        for language, (name, description) in config["copy"].items()
    }
