from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .entitlements import PREMIUM_DESIGNS_KEY, configure_plan_entitlements, plan_entitlement_snapshot
from .models import EntitlementDefinition, PlanEntitlement, SubscriptionPlan
from .policies import definition_contract_errors, get_business_rule


class EntitlementDefinitionSerializer(serializers.ModelSerializer):
    used_by_plans = serializers.IntegerField(source="plan_values.count", read_only=True)
    business_rule = serializers.SerializerMethodField()

    class Meta:
        model = EntitlementDefinition
        fields = [
            "id", "key", "name", "description", "translations", "value_type", "scope", "unit",
            "allowed_values", "default_value", "is_active", "sort_order", "used_by_plans", "created_at", "updated_at",
            "business_rule",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_key(self, value):
        if self.instance and value != self.instance.key:
            raise serializers.ValidationError("Capability keys are immutable; archive this capability and create a new key.")
        return value

    def get_business_rule(self, instance):
        rule = get_business_rule(instance.key)
        return rule.as_dict(configured=True) if rule else None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        key = attrs.get("key", self.instance.key if self.instance else "")
        value_type = attrs.get("value_type", self.instance.value_type if self.instance else "")
        scope = attrs.get("scope", self.instance.scope if self.instance else "")
        contract_errors = definition_contract_errors(key=key, value_type=value_type, scope=scope)
        rule = get_business_rule(key)
        is_active = attrs.get("is_active", self.instance.is_active if self.instance else True)
        if rule and rule.required and not is_active:
            contract_errors["is_active"] = "This core plan promise cannot be turned off."
        if contract_errors:
            raise serializers.ValidationError(contract_errors)
        if self.instance and PlanEntitlement.objects.filter(definition=self.instance).exists():
            contract_changes = {
                field: "This contract field is immutable after a capability is assigned to a plan."
                for field in ("value_type", "scope")
                if field in attrs and attrs[field] != getattr(self.instance, field)
            }
            if contract_changes:
                raise serializers.ValidationError(contract_changes)
        values = {
            field.name: attrs.get(
                field.name,
                getattr(self.instance, field.name) if self.instance else field.get_default(),
            )
            for field in EntitlementDefinition._meta.fields
            if field.name not in {"id", "created_at", "updated_at", "updated_by"}
        }
        candidate = EntitlementDefinition(**values)
        try:
            candidate.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}) from exc
        return attrs


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    class SubscriberTransition:
        NEW_ONLY = "new_only"
        EXISTING_AND_NEW = "existing_and_new"
        CHOICES = (NEW_ONLY, EXISTING_AND_NEW)

    is_free = serializers.BooleanField(read_only=True)
    entitlements = serializers.DictField(child=serializers.JSONField(), required=False)
    benefits = serializers.SerializerMethodField()
    subscriber_transition = serializers.ChoiceField(
        choices=SubscriberTransition.CHOICES,
        write_only=True,
        required=False,
        default=SubscriberTransition.NEW_ONLY,
    )

    class Meta:
        model = SubscriptionPlan
        fields = [
            "id", "name", "slug", "description", "translations", "price", "currency", "duration_days",
            "entitlement_scope", "features", "grants_premium_designs", "is_active",
            "is_featured", "sort_order", "upgrade_from", "revision", "entitlements", "benefits", "is_free", "created_at", "updated_at",
            "subscriber_transition",
        ]
        read_only_fields = ["id", "revision", "is_free", "created_at", "updated_at"]

    def _entitlement_snapshot(self, instance):
        cache = self.context.setdefault("plan_entitlement_snapshots", {})
        if instance.pk not in cache:
            cache[instance.pk] = plan_entitlement_snapshot(
                instance,
                definitions=self.context.get("entitlement_definitions"),
            )
        return cache[instance.pk]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["entitlements"] = self._entitlement_snapshot(instance)
        return data

    def get_benefits(self, instance):
        values = self._entitlement_snapshot(instance)
        definitions = self.context.get("entitlement_definitions")
        if definitions is None:
            definitions = list(EntitlementDefinition.objects.filter(is_active=True))
        benefits = []
        for definition in definitions:
            value = values.get(definition.key, definition.default_value)
            if definition.value_type == EntitlementDefinition.ValueType.BOOLEAN:
                if not value:
                    continue
            translations = {
                language: self._localized_benefit(definition, value, language)
                for language in ("en", "fr", "ar")
            }
            display = translations["en"]["display"]
            rule = get_business_rule(definition.key)
            benefits.append({
                "key": definition.key,
                "name": translations["en"]["name"],
                "description": translations["en"]["description"],
                "value": value,
                "unit": definition.unit,
                "display": display,
                "translations": translations,
                "scope": definition.scope,
                "counting_rules": rule.counting_rules if rule else "",
                "reset_window": rule.reset_window if rule else "none",
                "missing_behavior": rule.missing_behavior if rule else "use the configured default",
                "zero_behavior": rule.zero_behavior if rule else "not applicable",
                "downgrade_behavior": rule.downgrade_behavior if rule else "",
            })
        return benefits

    @staticmethod
    def _localized_benefit(definition, value, language):
        copy = definition.localized_copy(language)
        units = {
            "en": {"stores": ("store", "stores"), "members": ("member", "members"), "products": ("product", "products"), "emails": ("email", "emails"), "locations": ("location", "locations")},
            "fr": {"stores": ("boutique", "boutiques"), "members": ("membre", "membres"), "products": ("produit", "produits"), "emails": ("e-mail", "e-mails"), "locations": ("emplacement", "emplacements")},
            "ar": {"stores": ("متجر", "متاجر"), "members": ("عضو", "أعضاء"), "products": ("منتج", "منتجات"), "emails": ("رسالة", "رسائل"), "locations": ("موقع", "مواقع")},
        }
        choices = {
            "en": {"essential": "Essential", "standard": "Standard", "advanced": "Advanced", "priority": "Priority", "dedicated": "Dedicated"},
            "fr": {"essential": "Essentiel", "standard": "Standard", "advanced": "Avancé", "priority": "Prioritaire", "dedicated": "Dédié"},
            "ar": {"essential": "أساسي", "standard": "قياسي", "advanced": "متقدم", "priority": "أولوية", "dedicated": "مخصص"},
        }
        if definition.value_type == EntitlementDefinition.ValueType.BOOLEAN:
            display = copy["name"]
        elif definition.value_type == EntitlementDefinition.ValueType.INTEGER:
            rule = get_business_rule(definition.key)
            if value == 0 and rule and rule.zero_means_unlimited:
                unlimited = {"en": "Unlimited", "fr": "Illimité", "ar": "غير محدود"}[language]
                display = f"{copy['name']}: {unlimited}"
            else:
                forms = units.get(language, {}).get(definition.unit)
                unit = forms[0 if value == 1 else 1] if forms else definition.unit
                display = f"{copy['name']}: {value}{f' {unit}' if unit else ''}"
        elif definition.value_type == EntitlementDefinition.ValueType.CHOICE:
            display = f"{copy['name']}: {choices.get(language, {}).get(str(value), value)}"
        else:
            display = f"{copy['name']}: {value}"
        return {**copy, "display": display}

    def validate_currency(self, value):
        return value.strip().upper()

    def validate_features(self, value):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise serializers.ValidationError("Features must be a list of non-empty labels.")
        return [item.strip() for item in value]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        translations = attrs.get("translations", getattr(self.instance, "translations", {}))
        if not isinstance(translations, dict):
            raise serializers.ValidationError({"translations": "Translations must be an object."})
        translation_errors = {}
        normalized_translations = {}
        for language in ("en", "fr", "ar"):
            copy = translations.get(language)
            if not isinstance(copy, dict):
                translation_errors[language] = "Provide the plan name and description."
                continue
            name = copy.get("name")
            description = copy.get("description")
            if not isinstance(name, str) or not name.strip():
                translation_errors[language] = "Provide the translated plan name."
                continue
            if not isinstance(description, str) or not description.strip():
                translation_errors[language] = "Provide the translated plan description."
                continue
            normalized_translations[language] = {
                "name": name.strip(),
                "description": description.strip(),
            }
        if translation_errors:
            raise serializers.ValidationError({"translations": translation_errors})
        attrs["translations"] = normalized_translations
        # Canonical fields remain the English commercial copy for payment
        # snapshots and clients that predate localized plan content.
        attrs["name"] = normalized_translations["en"]["name"]
        attrs["description"] = normalized_translations["en"]["description"]
        price = attrs.get("price", getattr(self.instance, "price", Decimal("0.00")))
        slug = attrs.get("slug", getattr(self.instance, "slug", ""))
        if self.instance and self.instance.slug == "free" and slug != "free":
            raise serializers.ValidationError({"slug": "The baseline Free plan slug cannot be changed."})
        if slug == "free":
            protected = {
                "price": (price, Decimal("0.00")),
                "grants_premium_designs": (attrs.get("grants_premium_designs", getattr(self.instance, "grants_premium_designs", False)), False),
                "is_active": (attrs.get("is_active", getattr(self.instance, "is_active", True)), True),
                "entitlement_scope": (attrs.get("entitlement_scope", getattr(self.instance, "entitlement_scope", SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES)), SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES),
            }
            errors = {field: "This value is fixed for the baseline Free plan." for field, (value, expected) in protected.items() if value != expected}
            if errors:
                raise serializers.ValidationError(errors)
            entitlement_values = attrs.get("entitlements", {})
            if entitlement_values.get(PREMIUM_DESIGNS_KEY) is True:
                raise serializers.ValidationError({"entitlements": {PREMIUM_DESIGNS_KEY: "The Free plan cannot grant premium designs."}})
        if price != Decimal("0.00") and price < Decimal("5.00"):
            raise serializers.ValidationError({"price": "Paid plans must cost at least 5.00."})

        entitlement_values = attrs.get("entitlements")
        if entitlement_values is not None:
            definitions = {item.key: item for item in EntitlementDefinition.objects.filter(is_active=True)}
            errors = {}
            for key, value in entitlement_values.items():
                definition = definitions.get(key)
                if definition is None:
                    errors[key] = "Unknown or inactive capability key."
                    continue
                try:
                    definition.validate_value(value)
                except DjangoValidationError as exc:
                    errors[key] = exc.messages
            if errors:
                raise serializers.ValidationError({"entitlements": errors})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        entitlements = validated_data.pop("entitlements", None)
        validated_data.pop("subscriber_transition", None)
        # Compatibility output only: premium access is configured through the
        # governed entitlement, never through this legacy cache field.
        validated_data.pop("grants_premium_designs", None)
        actor = self.context.get("request").user if self.context.get("request") else None
        plan = super().create(validated_data)
        # Materialize defaults so later registry-default edits only affect new
        # plans, never silently rewrite existing offers.
        values = plan_entitlement_snapshot(plan)
        values.update(entitlements or {})
        configure_plan_entitlements(plan, values, actor=actor, increment_revision=False)
        if plan.is_active and plan.upgrade_from_id:
            from .progression import validate_plan_progression
            try:
                validate_plan_progression(plan)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.message_dict) from exc
        return plan

    @transaction.atomic
    def update(self, instance, validated_data):
        entitlements = validated_data.pop("entitlements", None)
        subscriber_transition = validated_data.pop(
            "subscriber_transition",
            self.SubscriberTransition.NEW_ONLY,
        )
        # Ignore stale/partial clients that submit the legacy field. Omitting
        # designs.premium must preserve the current promise; explicitly
        # changing that entitlement is the only supported write path.
        validated_data.pop("grants_premium_designs", None)
        actor = self.context.get("request").user if self.context.get("request") else None
        plan = super().update(instance, validated_data)
        values = dict(entitlements or {})
        configure_plan_entitlements(plan, values, actor=actor, increment_revision=True)
        if plan.is_active and plan.upgrade_from_id:
            from .progression import validate_plan_progression
            try:
                validate_plan_progression(plan)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(exc.message_dict) from exc
        if subscriber_transition == self.SubscriberTransition.EXISTING_AND_NEW:
            # Import at execution time to keep the entitlement/service modules
            # acyclic while reusing the canonical store-cache reconciliation.
            from .services import apply_plan_revision_to_active_subscriptions

            apply_plan_revision_to_active_subscriptions(plan)
        return plan


class CheckoutSerializer(serializers.Serializer):
    plan_id = serializers.IntegerField(min_value=1)
    provider = serializers.CharField(required=False, max_length=40)
    store_id = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    use_balance = serializers.BooleanField(default=False)
    idempotency_key = serializers.UUIDField(required=False)
    lang = serializers.ChoiceField(choices=("en", "fr", "ar"), default="ar")
    return_origin = serializers.URLField(required=False, max_length=300)

    def validate_provider(self, value):
        return value.strip().lower()
