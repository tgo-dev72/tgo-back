"""Cloudflare Email Routing for store aliases on one email-only subdomain."""

import requests
from django.conf import settings


class EmailRoutingError(Exception):
    pass


def alias_for(store):
    return f"{store.slug}@{settings.STORE_EMAIL_DOMAIN}"


def configured():
    return bool(
        settings.CLOUDFLARE_EMAIL_ROUTING_TOKEN
        and settings.CLOUDFLARE_ACCOUNT_ID
        and settings.CLOUDFLARE_ZONE_ID
        and settings.STORE_EMAIL_DOMAIN
    )


def _request(method, path, *, params=None, payload=None):
    if not configured():
        raise EmailRoutingError("Store email forwarding is not configured yet.")
    try:
        response = requests.request(
            method,
            f"https://api.cloudflare.com/client/v4{path}",
            headers={
                "Authorization": f"Bearer {settings.CLOUDFLARE_EMAIL_ROUTING_TOKEN}",
                "Content-Type": "application/json",
            },
            params=params,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EmailRoutingError("Email routing provider is unavailable. Try again later.") from exc
    if not body.get("success"):
        raise EmailRoutingError("Email routing provider rejected the request. Check its configuration.")
    return body


def _list(path):
    page = 1
    while True:
        body = _request("GET", path, params={"page": page, "per_page": 50})
        yield from body.get("result") or []
        info = body.get("result_info") or {}
        if page >= info.get("total_pages", 1):
            break
        page += 1


def destination_for(email):
    path = f"/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses"
    for address in _list(path):
        if address.get("email", "").casefold() == email.casefold():
            return address
    return None


def ensure_destination(email):
    address = destination_for(email)
    if address:
        return address
    path = f"/accounts/{settings.CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses"
    return _request("POST", path, payload={"email": email}).get("result") or {}


def _rule_for_alias(alias):
    path = f"/zones/{settings.CLOUDFLARE_ZONE_ID}/email/routing/rules"
    for rule in _list(path):
        if any(
            matcher.get("type") == "literal" and matcher.get("field") == "to"
            and matcher.get("value", "").casefold() == alias.casefold()
            for matcher in rule.get("matchers") or []
        ):
            return rule
    return None


def activate(store, destination):
    """Only call after Cloudflare says the destination is verified."""
    alias = alias_for(store)
    existing = _rule_for_alias(alias)
    if existing and not existing.get("name", "").startswith("TijaraGO store "):
        raise EmailRoutingError("This alias already has a routing rule outside TijaraGO.")
    payload = {
        "name": f"TijaraGO store {store.pk}",
        "enabled": True,
        "matchers": [{"type": "literal", "field": "to", "value": alias}],
        "actions": [{"type": "forward", "value": [destination]}],
    }
    path = f"/zones/{settings.CLOUDFLARE_ZONE_ID}/email/routing/rules"
    if existing:
        if existing.get("name") != payload["name"]:
            raise EmailRoutingError("This alias belongs to another store rule.")
        path += f"/{existing['id']}"
        method = "PUT"
    else:
        method = "POST"
    result = _request(method, path, payload=payload).get("result") or {}
    return result.get("id") or (existing or {}).get("id", "")


def disable(store):
    alias = alias_for(store)
    existing = _rule_for_alias(alias)
    if not existing:
        return
    if existing.get("name") != f"TijaraGO store {store.pk}":
        raise EmailRoutingError("This alias has a routing rule outside this store.")
    _request("DELETE", f"/zones/{settings.CLOUDFLARE_ZONE_ID}/email/routing/rules/{existing['id']}")
