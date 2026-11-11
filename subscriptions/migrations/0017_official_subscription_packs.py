from decimal import Decimal

from django.db import migrations


PLANS = {
    "free": (Decimal("0.00"), 0, False, {"stores.max_count": 1, "designs.premium": False, "team.max_members": 1, "catalog.max_products": 50, "domains.custom": False, "branding.remove_platform": False, "invoicing.issue": False, "analytics.level": "essential", "marketing.email_monthly_sends": 0, "catalog.import": False}),
    "starter": (Decimal("79.00"), 10, False, {"stores.max_count": 1, "designs.premium": False, "team.max_members": 2, "catalog.max_products": 500, "domains.custom": True, "branding.remove_platform": False, "invoicing.issue": True, "analytics.level": "standard", "marketing.email_monthly_sends": 200, "catalog.import": True}),
    "growth": (Decimal("149.00"), 20, True, {"stores.max_count": 3, "designs.premium": True, "team.max_members": 5, "catalog.max_products": 2000, "domains.custom": True, "branding.remove_platform": True, "invoicing.issue": True, "analytics.level": "advanced", "marketing.email_monthly_sends": 1000, "catalog.import": True}),
    "business": (Decimal("249.00"), 30, False, {"stores.max_count": 5, "designs.premium": True, "team.max_members": 10, "catalog.max_products": 10000, "domains.custom": True, "branding.remove_platform": True, "invoicing.issue": True, "analytics.level": "advanced", "marketing.email_monthly_sends": 3000, "catalog.import": True}),
}
COPY = {
    "free": {"en": {"name": "Free", "description": "Start selling online without an initial subscription."}, "fr": {"name": "Gratuit", "description": "Commencez à vendre en ligne sans abonnement initial."}, "ar": {"name": "مجاني", "description": "بدا البيع أونلاين بلا اشتراك فالبداية."}},
    "starter": {"en": {"name": "Starter", "description": "Essential tools for a professional online store."}, "fr": {"name": "Starter", "description": "Les outils essentiels pour professionnaliser votre boutique."}, "ar": {"name": "Starter", "description": "الأدوات الأساسية باش تطوّر متجرك."}},
    "growth": {"en": {"name": "Growth", "description": "Grow your brand and business."}, "fr": {"name": "Growth", "description": "Développez votre marque et votre activité."}, "ar": {"name": "Growth", "description": "طوّر علامتك التجارية وكبّر نشاطك."}},
    "business": {"en": {"name": "Business", "description": "More capacity for your growing business."}, "fr": {"name": "Business", "description": "Plus de capacité pour accompagner votre croissance."}, "ar": {"name": "Business", "description": "إمكانيات أكبر باش تواكب نمو تجارتك."}},
}
DEFINITIONS = {
    "stores.max_count": ("Stores included", "integer", "account", "stores", 1, []),
    "designs.premium": ("Premium designs", "boolean", "store", "", False, []),
    "team.max_members": ("Team members", "integer", "account", "members", 1, []),
    "catalog.max_products": ("Products per store", "integer", "store", "products", 100, []),
    "domains.custom": ("Custom domain", "boolean", "store", "", False, []),
    "branding.remove_platform": ("Remove platform branding", "boolean", "store", "", False, []),
    "invoicing.issue": ("Invoice issuance", "boolean", "store", "", False, []),
    "analytics.level": ("Analytics", "choice", "account", "", "essential", ["essential", "standard", "advanced"]),
    "marketing.email_monthly_sends": ("Monthly marketing emails", "integer", "store", "emails", 100, []),
    "catalog.import": ("Catalog import", "boolean", "store", "", False, []),
}


def seed_official_packs(apps, schema_editor):
    Plan = apps.get_model("subscriptions", "SubscriptionPlan")
    Definition = apps.get_model("subscriptions", "EntitlementDefinition")
    Value = apps.get_model("subscriptions", "PlanEntitlement")
    Commerce = apps.get_model("platform_admin", "PlatformCommerceSettings")
    definitions = {}
    for order, (key, (name, value_type, scope, unit, default, allowed)) in enumerate(DEFINITIONS.items(), start=1):
        definition, _ = Definition.objects.get_or_create(key=key, defaults={
            "name": name, "description": "Official TijaraGO subscription capability.",
            "value_type": value_type, "scope": scope, "unit": unit,
            "allowed_values": allowed, "default_value": default,
            "is_active": True, "sort_order": order * 5,
        })
        definitions[key] = definition
    previous = None
    for slug, (price, order, featured, values) in PLANS.items():
        translations = COPY[slug]
        plan, _ = Plan.objects.update_or_create(slug=slug, defaults={
            "name": translations["en"]["name"], "description": translations["en"]["description"],
            "translations": translations, "price": price, "currency": "MAD", "duration_days": 30,
            "entitlement_scope": "all_owned_stores", "is_active": True, "is_featured": featured,
            "sort_order": order, "upgrade_from": previous,
        })
        for key, value in values.items():
            Value.objects.update_or_create(plan=plan, definition=definitions[key], defaults={"value": value})
        previous = plan
    Plan.objects.filter(slug="premium").update(is_active=False, is_featured=False)
    Commerce.objects.filter(singleton_key=1).update(free_trial_days=30)


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0016_subscriptionlifecyclenotice"), ("platform_admin", "0006_platformcommercesettings")]
    operations = [migrations.RunPython(seed_official_packs, migrations.RunPython.noop)]
