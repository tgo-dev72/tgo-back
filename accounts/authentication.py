from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from secrets import compare_digest
from rest_framework.authentication import CSRFCheck
from rest_framework_simplejwt.authentication import JWTAuthentication

from config.middleware import PlatformOriginCsrfMixin

from .cookies import access_cookie_names, resolve_tenant
from .access import has_merchant_access
from .tokens import CUSTOMER_SCOPE, MERCHANT_SCOPE, PLATFORM_SCOPE


def _csrf_protected_view(request):
    """Sentinel view used only to run CsrfViewMiddleware's validation."""


class PlatformCSRFCheck(PlatformOriginCsrfMixin, CSRFCheck):
    pass


def _enforce_csrf(request):
    """Apply Django's CSRF checks to credentials supplied by a cookie.

    DRF's JWT authentication intentionally does not do this by default,
    because JWTs are normally sent in an Authorization header. Here they
    are browser cookies, so every unsafe authenticated request must carry
    a valid CSRF token.
    """
    check = PlatformCSRFCheck(lambda request: None)
    check.process_request(request)
    reason = check.process_view(request, _csrf_protected_view, (), {})
    if reason:
        raise PermissionDenied(f"CSRF Failed: {reason}")


class ScopedCookieJWTAuthentication(JWTAuthentication):
    """Base authenticator for the role-scoped cookie names in cookies.py.

    Tries each of ``scopes`` in order until a valid, authorized JWT is found
    in its cookie. Client scopes resolve the tenant from the request. CSRF is
    enforced once a cookie credential is accepted.
    """
    scopes = ()
    token_scopes = {
        'staff': PLATFORM_SCOPE,
        'owner': MERCHANT_SCOPE,
        'client': CUSTOMER_SCOPE,
    }

    def authenticate(self, request):
        tenant = resolve_tenant(request) if 'client' in self.scopes else None
        credential_seen = False
        for scope in self.scopes:
            names = access_cookie_names(scope, tenant if scope == 'client' else None)
            for access_name in names:
                access_token = request.COOKIES.get(access_name)
                if not access_token:
                    continue
                credential_seen = True
                try:
                    validated_token = self.get_validated_token(access_token)
                    user = self.get_user(validated_token)
                except AuthenticationFailed:
                    continue
                if validated_token.get('auth_scope') != self.token_scopes[scope]:
                    continue
                if not compare_digest(str(validated_token.get("password_state", "")), user.get_session_auth_hash()):
                    continue
                if not self.authorized(user, validated_token, tenant):
                    continue
                _enforce_csrf(request)
                return user, validated_token
        if credential_seen:
            raise AuthenticationFailed("This session is not valid for this application.")
        return None

    def authorized(self, user, token, tenant=None):
        return True


class StaffCookieJWTAuthentication(ScopedCookieJWTAuthentication):
    """Platform team — reads the neutral platform-session cookie slot."""
    scopes = ('staff',)

    def authorized(self, user, token, tenant=None):
        return bool(getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False))


class OwnerCookieJWTAuthentication(ScopedCookieJWTAuthentication):
    """Merchant dashboard — reads the neutral merchant-session cookie slot.

    Accepts any authenticated user: authorization is enforced per-view through
    StoreMember query scoping, and a merchant's first store is created (which
    makes them an owner) before any membership row exists.
    """
    scopes = ('owner',)

    def authorized(self, user, token, tenant=None):
        return has_merchant_access(user)

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        recovery_prefixes = ("/api/subscriptions/", "/api/auth/", "/accounts/")
        shell_store_list = request.method == "GET" and request.path == "/api/store/"
        if not request.path.startswith(recovery_prefixes) and not shell_store_list:
            from subscriptions.access import enforce_merchant_subscription
            enforce_merchant_subscription(user, request)
        return user, token


class ClientCookieJWTAuthentication(ScopedCookieJWTAuthentication):
    """Storefront customer — reads a neutral tenant-isolated cookie slot."""
    scopes = ('client',)

    def authorized(self, user, token, tenant=None):
        if not tenant or token.get('tenant') != tenant:
            return False
        from store.models import StoreCustomer

        return StoreCustomer.objects.filter(
            profile__user=user,
            store__slug=tenant,
        ).exists()
