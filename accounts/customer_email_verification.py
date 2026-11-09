"""Signed, expiring customer email verification bound to one storefront."""

import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from accounts.models import AuthUser
from store.models import StoreCustomer


TOKEN_SALT = "accounts.customer-email-verification.v1"


class InvalidCustomerEmailVerificationToken(Exception):
    def __init__(self, reason="invalid"):
        self.reason = reason
        super().__init__(reason)


def make_customer_email_verification_token(user, store):
    return signing.dumps(
        {"user_id": user.pk, "email": user.email, "store": store.slug},
        key=settings.SECRET_KEY,
        salt=TOKEN_SALT,
        compress=True,
    )


def customer_storefront_url(store):
    from storefront_factory.domains import public_storefront_url

    configured = public_storefront_url(store).strip()
    origin = configured if configured.startswith(("http://", "https://")) else f"https://{configured}"
    return origin.rstrip("/")


def build_customer_email_verification_url(user, store):
    query = urlencode({"verify": make_customer_email_verification_token(user, store)})
    return f"{customer_storefront_url(store)}/account?{query}"


@transaction.atomic
def verify_customer_email(token, *, expected_store_slug=""):
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
        store_slug = payload["store"]
    except signing.SignatureExpired as exc:
        raise InvalidCustomerEmailVerificationToken("expired") from exc
    except signing.BadSignature as exc:
        raise InvalidCustomerEmailVerificationToken("bad_signature") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCustomerEmailVerificationToken("malformed_payload") from exc

    if expected_store_slug and not secrets.compare_digest(store_slug, expected_store_slug):
        raise InvalidCustomerEmailVerificationToken("store_mismatch")

    try:
        user = AuthUser.objects.select_for_update().get(pk=user_id)
        customer = StoreCustomer.objects.select_related("profile", "store").get(
            profile__user=user,
            store__slug=store_slug,
        )
    except (AuthUser.DoesNotExist, StoreCustomer.DoesNotExist) as exc:
        raise InvalidCustomerEmailVerificationToken("account_missing") from exc

    if not secrets.compare_digest(user.email, signed_email):
        raise InvalidCustomerEmailVerificationToken("account_mismatch")

    if user.email_verified_at is not None:
        if not user.is_active:
            raise InvalidCustomerEmailVerificationToken("account_unavailable")
        return user, customer, False

    user.email_verified_at = timezone.now()
    user.is_active = True
    user.save(update_fields=["email_verified_at", "is_active"])
    return user, customer, True
