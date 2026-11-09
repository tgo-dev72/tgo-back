"""Signed, expiring, and idempotent merchant email verification."""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from .models import AuthUser


TOKEN_SALT = "accounts.merchant-email-verification.v1"


class InvalidEmailVerificationToken(Exception):
    """The supplied token cannot authorize an email address."""

    def __init__(self, reason="invalid"):
        self.reason = reason
        super().__init__(reason)


def make_email_verification_token(user):
    return signing.dumps(
        {"user_id": user.pk, "email": user.email},
        key=settings.SECRET_KEY,
        salt=TOKEN_SALT,
        compress=True,
    )


def build_email_verification_url(user, frontend_url=None):
    query = urlencode({"token": make_email_verification_token(user)})
    target = (frontend_url or settings.EMAIL_VERIFICATION_FRONTEND_URL).rstrip("/")
    return f"{target}?{query}"


@transaction.atomic
def verify_merchant_email(token):
    try:
        payload = signing.loads(
            token,
            key=settings.SECRET_KEY,
            salt=TOKEN_SALT,
            max_age=settings.EMAIL_VERIFICATION_TIMEOUT_SECONDS,
            fallback_keys=settings.SECRET_KEY_FALLBACKS,
        )
        user_id = payload["user_id"]
        signed_email = payload["email"]
    except signing.SignatureExpired as exc:
        raise InvalidEmailVerificationToken("expired") from exc
    except signing.BadSignature as exc:
        raise InvalidEmailVerificationToken("bad_signature") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidEmailVerificationToken("malformed_payload") from exc

    try:
        user = (
            AuthUser.objects.select_for_update()
            .get(pk=user_id)
        )
    except AuthUser.DoesNotExist as exc:
        raise InvalidEmailVerificationToken("account_missing") from exc

    if not hasattr(user, "merchant_profile") or not secrets.compare_digest(user.email, signed_email):
        raise InvalidEmailVerificationToken("account_mismatch")

    # Successful replays remain successful but never reactivate an account that
    # was subsequently disabled by a platform operator.
    if user.email_verified_at is not None:
        if user.is_active:
            from subscriptions.services import ensure_free_subscription

            ensure_free_subscription(user)
        return user, False

    user.email_verified_at = timezone.now()
    user.is_active = True
    user.save(update_fields=["email_verified_at", "is_active"])
    from subscriptions.services import ensure_free_subscription

    ensure_free_subscription(user)
    return user, True
