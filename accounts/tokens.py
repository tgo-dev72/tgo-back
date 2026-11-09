"""Audience-bound JWT issuance for the platform's three application surfaces."""

from rest_framework_simplejwt.tokens import RefreshToken


PLATFORM_SCOPE = "platform"
MERCHANT_SCOPE = "merchant"
CUSTOMER_SCOPE = "customer"


def issue_scoped_tokens(user, scope, tenant=None):
    refresh = RefreshToken.for_user(user)
    refresh["auth_scope"] = scope
    refresh["password_state"] = user.get_session_auth_hash()
    if tenant:
        refresh["tenant"] = tenant
    return str(refresh.access_token), str(refresh)
