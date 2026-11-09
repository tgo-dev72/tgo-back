"""Resolves which Store a public request belongs to.

Uses the same tenant selection as customer authentication: explicit store
header, or a storefront subdomain under the configured platform domain.
"""
from django.http import Http404
from accounts.cookies import resolve_tenant

from .models import Store


def resolve_store(request):
    slug = resolve_tenant(request)
    if not slug:
        raise Http404("No store resolved for this request.")
    store = Store.objects.filter(slug=slug).first()
    if store is None:
        raise Http404(f"Store '{slug}' not found.")
    return store
