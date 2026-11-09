from django.db import migrations, models

import subscriptions.models


KNOWN_TRANSLATIONS = {
    "free": {
        "en": {
            "name": "Free",
            "description": "Start selling with the essential tools for one online store.",
        },
        "fr": {
            "name": "Gratuit",
            "description": "Commencez à vendre avec les outils essentiels pour une boutique en ligne.",
        },
        "ar": {
            "name": "مجاني",
            "description": "ابدأ البيع باستخدام الأدوات الأساسية لمتجر إلكتروني واحد.",
        },
    },
    "premium": {
        "en": {
            "name": "Premium",
            "description": "Advanced storefront workflows and premium designs.",
        },
        "fr": {
            "name": "Premium",
            "description": "Des parcours de vente avancés et des designs premium.",
        },
        "ar": {
            "name": "بريميوم",
            "description": "مسارات بيع متقدمة وتصاميم مميزة.",
        },
    },
}


def populate_plan_translations(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all().iterator():
        translations = KNOWN_TRANSLATIONS.get(plan.slug)
        if translations is None:
            # Existing custom packs cannot be translated safely in a data
            # migration. Keep their current copy visible in every language
            # until an operator publishes real translations.
            translations = {
                language: {"name": plan.name, "description": plan.description}
                for language in ("en", "fr", "ar")
            }
        plan.translations = translations
        plan.name = translations["en"]["name"]
        plan.description = translations["en"]["description"]
        plan.save(update_fields=["translations", "name", "description"])


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0009_assign_verified_merchants_to_free"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="translations",
            field=models.JSONField(blank=True, default=subscriptions.models.default_plan_translations),
        ),
        migrations.RunPython(populate_plan_translations, migrations.RunPython.noop),
    ]
