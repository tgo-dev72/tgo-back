"""Shared validation for merchant-authored metadata."""

from django.core.exceptions import ValidationError
from django.utils.html import strip_tags


def clean_seo_text(value, *, label):
    value = str(value or "").strip()
    if value != strip_tags(value):
        raise ValidationError(f"{label} must be plain text without HTML.")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValidationError(f"{label} contains unsupported control characters.")
    return value
