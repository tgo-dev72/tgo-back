"""Semantic validation for explicitly configured commercial upgrade paths."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from .entitlements import plan_entitlement_snapshot
from .models import EntitlementDefinition
from .policies import (
    ANALYTICS_LEVEL_KEY,
    CATALOG_IMPORT_KEY,
    CUSTOM_DOMAIN_KEY,
    EMAIL_SEND_LIMIT_KEY,
    INVOICING_KEY,
    PREMIUM_DESIGNS_KEY,
    PRODUCT_LIMIT_KEY,
    REMOVE_BRANDING_KEY,
    STORE_LIMIT_KEY,
    TEAM_LIMIT_KEY,
    get_business_rule,
)


PROGRESSION_KEYS = (
    STORE_LIMIT_KEY,
    TEAM_LIMIT_KEY,
    PRODUCT_LIMIT_KEY,
    CATALOG_IMPORT_KEY,
    PREMIUM_DESIGNS_KEY,
    CUSTOM_DOMAIN_KEY,
    REMOVE_BRANDING_KEY,
    INVOICING_KEY,
    ANALYTICS_LEVEL_KEY,
    EMAIL_SEND_LIMIT_KEY,
)
ANALYTICS_ORDER = {"essential": 0, "standard": 1, "advanced": 2}


@dataclass(frozen=True)
class ProgressionConflict:
    key: str
    lower_plan: str
    higher_plan: str
    lower_scope: str
    higher_scope: str
    lower_value: object
    higher_value: object
    reason: str

    def message(self):
        return (
            f"{self.key}: {self.higher_plan} ({self.higher_scope}, {self.higher_value!r}) "
            f"cannot upgrade {self.lower_plan} ({self.lower_scope}, {self.lower_value!r}): {self.reason}."
        )


def _is_reduction(definition, lower, higher):
    if definition.value_type == EntitlementDefinition.ValueType.BOOLEAN:
        return bool(lower) and not bool(higher)
    if definition.value_type == EntitlementDefinition.ValueType.INTEGER:
        rule = get_business_rule(definition.key)
        lower_number, higher_number = int(lower), int(higher)
        if rule and rule.zero_means_unlimited:
            if lower_number == 0:
                return higher_number != 0
            if higher_number == 0:
                return False
        return higher_number < lower_number
    if definition.key == ANALYTICS_LEVEL_KEY:
        return ANALYTICS_ORDER.get(str(higher), -1) < ANALYTICS_ORDER.get(str(lower), -1)
    return lower != higher


def compare_plan_progression(lower_plan, higher_plan):
    definitions = {
        item.key: item
        for item in EntitlementDefinition.objects.filter(key__in=PROGRESSION_KEYS, is_active=True)
    }
    lower_values = plan_entitlement_snapshot(lower_plan, definitions=definitions.values())
    higher_values = plan_entitlement_snapshot(higher_plan, definitions=definitions.values())
    conflicts = []
    if lower_plan.entitlement_scope != higher_plan.entitlement_scope:
        conflicts.append(ProgressionConflict(
            key="entitlement_scope",
            lower_plan=lower_plan.slug,
            higher_plan=higher_plan.slug,
            lower_scope=lower_plan.entitlement_scope,
            higher_scope=higher_plan.entitlement_scope,
            lower_value=lower_plan.entitlement_scope,
            higher_value=higher_plan.entitlement_scope,
            reason="plans in one upgrade path must use the same subscription coverage scope",
        ))
        return conflicts
    for key in PROGRESSION_KEYS:
        definition = definitions.get(key)
        if definition is None:
            conflicts.append(ProgressionConflict(
                key=key, lower_plan=lower_plan.slug, higher_plan=higher_plan.slug,
                lower_scope="missing", higher_scope="missing", lower_value=None, higher_value=None,
                reason="the authoritative entitlement definition is missing or inactive",
            ))
            continue
        lower = lower_values.get(key, definition.default_value)
        higher = higher_values.get(key, definition.default_value)
        if _is_reduction(definition, lower, higher):
            conflicts.append(ProgressionConflict(
                key=key,
                lower_plan=lower_plan.slug,
                higher_plan=higher_plan.slug,
                lower_scope=definition.scope,
                higher_scope=definition.scope,
                lower_value=lower,
                higher_value=higher,
                reason="the higher plan reduces an included capability or allowance",
            ))
    return conflicts


def validate_plan_progression(plan):
    if not plan.upgrade_from_id:
        return []
    seen = {plan.pk}
    predecessor = plan.upgrade_from
    while predecessor is not None:
        if predecessor.pk in seen:
            raise ValidationError({"upgrade_from": ["The explicit upgrade path contains a cycle."]})
        seen.add(predecessor.pk)
        predecessor = predecessor.upgrade_from
    conflicts = compare_plan_progression(plan.upgrade_from, plan)
    if conflicts:
        raise ValidationError({"upgrade_from": [item.message() for item in conflicts]})
    return []
