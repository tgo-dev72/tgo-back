"""Short-lived capability tickets for official read-only storefront previews."""

from django.conf import settings
from django.core import signing


SALT = "tijarago.theme-preview.v1"


def issue_preview_ticket(*, theme_key: str, store_slug: str) -> str:
    return signing.dumps({"theme": theme_key, "store": store_slug}, salt=SALT, compress=True)


def validate_preview_ticket(ticket: str, *, theme_key: str, store_slug: str) -> bool:
    if not ticket:
        return False
    try:
        payload = signing.loads(
            ticket,
            salt=SALT,
            max_age=getattr(settings, "THEME_PREVIEW_TICKET_MAX_AGE", 600),
        )
    except signing.BadSignature:
        return False
    return payload == {"theme": theme_key, "store": store_slug}
