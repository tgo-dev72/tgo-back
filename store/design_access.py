"""Storefront design entitlement rules.

Keep this module independent from HTTP serializers so every entry point
(store creation, configuration updates, deployment requests, and runtime
configuration) applies the same rule.
"""

CURRENT_DESIGN_KEYS = frozenset({"basic", "beauty", "fashion", "electronics", "nexa"})
PREMIUM_DESIGN_KEYS = frozenset({"beauty", "fashion", "electronics", "nexa", "atelier", "variants", "discovery", "story", "conversion", "trade"})
FREE_FALLBACK_DESIGN = "basic"
READY_DESIGN_FALLBACK_KEYS = frozenset({"basic"})
LEGACY_DESIGN_KEYS = frozenset({
    "atelier",
    "variants", "discovery", "story", "conversion", "trade",
    "nova", "ember", "flux", "aura", "meridian", "zenith",
    "solstice", "polars", "equinox", "atlas", "vertex", "nexus",
})


def is_ready_design(design: str) -> bool:
    try:
        from api.designs import get_design

        return get_design(design).get("status") == "ready"
    except Exception:
        return design in READY_DESIGN_FALLBACK_KEYS


def is_premium_design(design: str) -> bool:
    # The manifest is the source of truth.  Keep the small fallback set so a
    # temporary manifest/read failure never turns a known paid design free.
    try:
        from api.designs import get_design

        return get_design(design).get("tier", "standard") == "premium"
    except Exception:
        return design in PREMIUM_DESIGN_KEYS


def can_use_design(plan: str, design: str) -> bool:
    if not is_ready_design(design):
        return False
    return plan == "premium" or not is_premium_design(design)


def effective_design(plan: str, design: str) -> str:
    """Return a design that the plan is entitled to render.

    The runtime check is deliberately fail-closed. It protects storefronts
    from inconsistent legacy rows or direct database writes that bypass API
    validation, while keeping the store online with the standard fallback.
    """
    # Existing deployments remain readable while the replacement catalogue is
    # rolled out. Legacy keys are not selectable for new stores because
    # can_use_design() still rejects them.
    if design in LEGACY_DESIGN_KEYS:
        return FREE_FALLBACK_DESIGN if plan != "premium" and is_premium_design(design) else design
    return design if can_use_design(plan, design) else FREE_FALLBACK_DESIGN
