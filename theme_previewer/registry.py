from dataclasses import dataclass
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.db import DatabaseError

from api.designs import DesignNotFound, get_design, list_designs
from .models import ThemeDemoStore
from .presets import presets_for_theme
from .tickets import issue_preview_ticket


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicThemePreview:
    theme_key: str
    store_slug: str
    public_url: str
    tier: str


def eligible_designs():
    """Manifest-driven list; database rows can only narrow public availability."""
    return [item for item in list_designs() if item.get("status") == "ready"]


def public_preview(theme_key: str) -> PublicThemePreview | None:
    try:
        design = get_design(theme_key)
    except DesignNotFound:
        return None
    if design.get("status") != "ready":
        return None
    try:
        profile = ThemeDemoStore.objects.select_related("store", "store__config").filter(
            theme_key=theme_key,
            is_enabled=True,
            deployment_status=ThemeDemoStore.DeploymentStatus.READY,
        ).first()
    except DatabaseError:
        # Theme previews are optional catalog metadata. During a staggered
        # deployment the application revision can briefly precede its schema
        # migration; keep the public design catalog usable and simply omit
        # previews until the migration is applied.
        logger.exception("Theme preview metadata is unavailable for %s", theme_key)
        return None
    if not profile or profile.store.config.design != theme_key:
        return None
    parsed = urlparse(profile.public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return PublicThemePreview(
        theme_key=theme_key,
        store_slug=profile.store.slug,
        public_url=profile.public_url,
        tier=design.get("tier", "standard"),
    )


def preview_payload(design: dict, *, include_ticket: bool = False) -> dict:
    preview = public_preview(str(design.get("key", "")))
    payload = {
        **design,
        "demo_url": preview.public_url if preview else None,
        "preview_available": preview is not None,
        "demo_store_slug": preview.store_slug if preview else None,
        "presets": presets_for_theme(str(design.get("key", ""))),
    }
    if include_ticket and preview:
        payload["preview_ticket"] = issue_preview_ticket(
            theme_key=preview.theme_key,
            store_slug=preview.store_slug,
        )
    return payload


def signup_url(theme_key: str) -> str:
    base = getattr(settings, "THEME_PREVIEW_SIGNUP_URL", "https://www.tijarago.com/signup")
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}design={theme_key}"
