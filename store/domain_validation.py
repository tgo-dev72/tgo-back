"""Strict hostname normalization for merchant-owned storefront domains."""
import ipaddress
import re

from django.conf import settings
from django.core.exceptions import ValidationError


LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RESERVED_SUFFIXES = (".local", ".localhost", ".internal", ".invalid", ".example", ".test", ".onion")
CONTROL_LABELS = {"api", "admin", "dash", "www", "store", "platform", "mail"}


def normalize_storefront_hostname(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().rstrip(".")
    if not raw or any(marker in raw for marker in ("://", "/", "?", "#", "@")):
        raise ValidationError("Enter a hostname only, without a scheme, path, port or credentials.")
    try:
        ipaddress.ip_address(raw.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValidationError("IP addresses cannot be used as storefront domains.")
    try:
        ascii_name = raw.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValidationError("Enter a valid international domain name.") from exc
    if len(ascii_name) > 253 or "." not in ascii_name:
        raise ValidationError("Enter a complete public domain name.")
    labels = ascii_name.split(".")
    if any(not LABEL_RE.fullmatch(label) for label in labels):
        raise ValidationError("The domain contains an invalid DNS label.")
    if ascii_name == "localhost" or ascii_name.endswith(RESERVED_SUFFIXES):
        raise ValidationError("Private, test and reserved domains are not supported.")
    if labels[0] in CONTROL_LABELS and ascii_name.endswith("." + settings.STOREFRONT_BASE_DOMAIN.strip(".").lower()):
        raise ValidationError("This TijaraGO control hostname is reserved.")
    platform_base = settings.STOREFRONT_BASE_DOMAIN.strip(".").lower()
    if ascii_name == platform_base or ascii_name.endswith("." + platform_base):
        raise ValidationError("TijaraGO platform addresses are assigned automatically.")
    # A maintained PSL implementation is preferred when installed. The fallback
    # still rejects a bare suffix rather than silently accepting it.
    try:
        import tldextract
        # Use the bundled PSL snapshot. A validation request must never trigger
        # an outbound suffix-list download or become dependent on its latency.
        extracted = tldextract.TLDExtract(
            suffix_list_urls=(), include_psl_private_domains=True, cache_dir=None,
        )(ascii_name)
        if not extracted.suffix or not extracted.domain:
            raise ValidationError("Enter a registrable public domain name.")
    except ImportError:
        if len(labels) < 2 or len(labels[-1]) < 2:
            raise ValidationError("Enter a registrable public domain name.")
    return ascii_name, raw
