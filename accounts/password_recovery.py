"""Hashed, single-use password recovery for existing AuthUser identities."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from .access import has_merchant_access
from .models import AuthUser, PasswordResetToken


def normalized_recovery_email(value):
    email = str(value or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return None
    return email if len(email) <= 254 else None


def _digest(raw):
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def is_eligible(user, scope, store=None):
    if not user.is_active or user.email_verified_at is None or not user.has_usable_password():
        return False
    if scope == "merchant":
        return has_merchant_access(user)
    if scope == "customer" and store:
        from store.models import StoreCustomer

        # One AuthUser owns one password across roles. A customer recovery
        # link must not change credentials of a merchant or platform operator.
        customer_stores = StoreCustomer.objects.filter(profile__user=user)
        return (
            not user.is_staff
            and not user.is_superuser
            and not has_merchant_access(user)
            and customer_stores.count() == 1
            and customer_stores.filter(store=store).exists()
        )
    return False


def issue_recovery(user, scope, store=None):
    """Return a raw token once; persist only its hash, with a per-account cooldown."""
    now = timezone.now()
    with transaction.atomic():
        locked = AuthUser.objects.select_for_update().get(pk=user.pk)
        if not is_eligible(locked, scope, store):
            return None
        if PasswordResetToken.objects.filter(
            user=locked, scope=scope, store=store,
            created_at__gt=now - timedelta(seconds=60),
        ).exists():
            return None
        raw = secrets.token_urlsafe(32)
        PasswordResetToken.objects.create(
            user=locked, scope=scope, store=store, token_hash=_digest(raw),
            expires_at=now + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT_SECONDS),
        )
    return raw


def consume_recovery(raw, password, scope, store=None):
    """Lock the identity so concurrent redemptions cannot both change its password."""
    if not isinstance(raw, str) or len(raw) > 256 or len(raw) < 32:
        return "invalid"
    digest = _digest(raw)
    with transaction.atomic():
        row = PasswordResetToken.objects.filter(token_hash=digest, scope=scope, store=store).values("user_id").first()
        if not row:
            return "invalid"
        user = AuthUser.objects.select_for_update().get(pk=row["user_id"])
        token = PasswordResetToken.objects.select_for_update().get(token_hash=digest)
        if token.consumed_at or token.expires_at <= timezone.now() or not is_eligible(user, scope, store):
            return "invalid"
        try:
            validate_password(password, user=user)
        except ValidationError:
            return "password"
        user.set_password(password)
        user.save(update_fields=["password"])
        PasswordResetToken.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
    return "ok"


def merchant_reset_url(raw, frontend_url):
    return f"{frontend_url.rstrip('/')}/reset-password#token={raw}"


def customer_reset_url(raw, store):
    from .customer_email_verification import customer_storefront_url

    return f"{customer_storefront_url(store)}/account#reset={raw}"
