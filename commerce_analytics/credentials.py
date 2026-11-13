import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    raw = getattr(settings, "META_CREDENTIAL_ENCRYPTION_KEY", "").strip()
    if not raw:
        raise ImproperlyConfigured("META_CREDENTIAL_ENCRYPTION_KEY is required for Meta CAPI credentials.")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, TypeError):
        # Permit a high-entropy deployment secret while still deriving a valid Fernet key.
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest()))


def encrypt_access_token(value):
    return _fernet().encrypt(value.strip().encode("utf-8")).decode("ascii")


def decrypt_access_token(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ImproperlyConfigured("Meta CAPI credential cannot be decrypted with the configured key.") from exc
