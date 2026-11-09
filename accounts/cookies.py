"""Audience-scoped, tenant-aware auth cookies with neutral browser names.

The platform serves three audiences on shared origins, so a single
``access_token`` cookie would be clobbered whenever someone works as more
than one of them:

  staff   → platform team (Django is_staff/is_superuser)
  owner   → merchant dashboard (registered merchants, StoreMember-scoped views)
  client  → storefront customers (ClientProfile), per store tenant

Internal scopes remain explicit in Python and JWT claims. Browser cookie names
use opaque slots so they do not advertise roles, token purpose, or storefront
identity. ``__Host-`` also prevents a sibling subdomain from shadowing them.
"""

import hashlib
import hmac

from django.conf import settings

SCOPE_ORDER = ('staff', 'owner', 'client')

SCOPE_COOKIES = {
    'staff': ('__Host-tgo-01', '__Host-tgo-02'),
    'owner': ('__Host-tgo-03', '__Host-tgo-04'),
    'client': ('__Host-tgo-05', '__Host-tgo-06'),
}

# Read-only compatibility for sessions issued before the neutral-name rollout.
# New logins and refreshes only write SCOPE_COOKIES. Logout clears both sets.
LEGACY_SCOPE_COOKIES = {
    'staff': ('staff_access_token', 'staff_refresh_token'),
    'owner': ('owner_access_token', 'owner_refresh_token'),
    'client': ('client_access_token', 'client_refresh_token'),
}

ACCESS_MAX_AGE = 3600 * 8
REFRESH_MAX_AGE = 3600 * 24

COOKIE_OPTS = dict(httponly=True, secure=True, samesite='None', path='/')
DELETE_OPTS = dict(samesite='None', path='/')

# Hosts that are platform-owned, never storefront tenants.
_PLATFORM_HOSTS = {'www', 'api', 'admin', 'store', 'platform'}


def _opaque_tenant_suffix(tenant):
    return hmac.new(
        settings.SECRET_KEY.encode('utf-8'),
        tenant.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:20]


def _cookie_names(mapping, scope, tenant=None, *, opaque_tenant=False):
    access, refresh = mapping[scope]
    if scope == 'client':
        if not tenant:
            return None, None
        suffix = _opaque_tenant_suffix(tenant) if opaque_tenant else tenant
        access = f'{access}-{suffix}'
        refresh = f'{refresh}-{suffix}'
    return access, refresh


def cookie_names(scope, tenant=None):
    return _cookie_names(SCOPE_COOKIES, scope, tenant, opaque_tenant=True)


def legacy_cookie_names(scope, tenant=None):
    access, refresh = _cookie_names(LEGACY_SCOPE_COOKIES, scope, tenant)
    if scope == 'client' and access:
        # Legacy names used an underscore before the clear-text tenant slug.
        access = access.rsplit('-', 1)[0] + f'_{tenant}'
        refresh = refresh.rsplit('-', 1)[0] + f'_{tenant}'
    return access, refresh


def cookie_name_candidates(scope, tenant=None):
    return cookie_names(scope, tenant), legacy_cookie_names(scope, tenant)


def access_cookie_names(scope, tenant=None):
    return tuple(pair[0] for pair in cookie_name_candidates(scope, tenant) if pair[0])


def refresh_cookie_names(scope, tenant=None):
    return tuple(pair[1] for pair in cookie_name_candidates(scope, tenant) if pair[1])


def access_cookie_name(scope, tenant=None):
    return cookie_names(scope, tenant)[0]


def refresh_cookie_name(scope, tenant=None):
    return cookie_names(scope, tenant)[1]


def resolve_tenant(request):
    """Resolve the storefront tenant for the request.

    Prefers the ``X-Store-Slug`` header (the storefront sends it in dev where
    no subdomain exists), then falls back to a ``<slug>.<base domain>`` host.
    """
    slug = (request.META.get('HTTP_X_STORE_SLUG') or '').strip().lower()
    if slug:
        return slug
    host = (request.META.get('HTTP_HOST') or '').split(':')[0].strip().lower()
    base = (getattr(settings, 'STOREFRONT_BASE_DOMAIN', '') or '').lower()
    if base and host.endswith('.' + base):
        sub = host[:-(len(base) + 1)]
        if sub and sub not in _PLATFORM_HOSTS:
            return sub
    return None


def set_auth_cookies(response, scope, access, refresh, tenant=None):
    access_name, refresh_name = cookie_names(scope, tenant)
    if not access_name:
        return response
    response.set_cookie(access_name, access, max_age=ACCESS_MAX_AGE, **COOKIE_OPTS)
    response.set_cookie(refresh_name, refresh, max_age=REFRESH_MAX_AGE, **COOKIE_OPTS)
    return response


def clear_backoffice_cookies(response):
    for scope in ('staff', 'owner'):
        for access, refresh in cookie_name_candidates(scope):
            response.delete_cookie(access, **DELETE_OPTS)
            response.delete_cookie(refresh, **DELETE_OPTS)
    return response


def clear_client_cookies(response, tenant):
    for access, refresh in cookie_name_candidates('client', tenant):
        if access:
            response.delete_cookie(access, **DELETE_OPTS)
            response.delete_cookie(refresh, **DELETE_OPTS)
    return response
