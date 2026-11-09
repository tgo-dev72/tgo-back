import re

from django.conf import settings

from store.email_routing import EmailRoutingError, _request, _rule_for_alias, alias_for


def configured():
    if settings.MAILBOX_TRANSPORT_MODE == "local":
        return bool(settings.DEBUG and settings.MAILBOX_INBOUND_SECRET)
    return bool(
        settings.CLOUDFLARE_EMAIL_ROUTING_TOKEN
        and settings.CLOUDFLARE_ZONE_ID
        and settings.CLOUDFLARE_EMAIL_WORKER_NAME
        and settings.MAILBOX_INBOUND_SECRET
    )


def activate_mailbox(store):
    if not configured():
        raise EmailRoutingError("The dashboard mailbox is not configured yet.")
    if settings.MAILBOX_TRANSPORT_MODE == "local":
        return f"local-mailbox-{store.pk}"
    alias = alias_for(store)
    existing = _rule_for_alias(alias)
    allowed_names = {f"TijaraGO store {store.pk}", f"TijaraGO mailbox {store.pk}"}
    if existing and existing.get("name") not in allowed_names:
        # A deleted test/store can leave its old forwarding rule behind while a
        # later store legitimately reuses the slug. Reclaim only TijaraGO-owned
        # legacy rules whose recorded store no longer exists. Active-store and
        # externally managed rules remain protected from takeover.
        match = re.fullmatch(r"TijaraGO store (\d+)", existing.get("name", ""))
        if not match:
            raise EmailRoutingError("This email alias is managed by another routing rule.")
        from store.models import Store
        if Store.objects.filter(pk=int(match.group(1))).exists():
            raise EmailRoutingError("This email alias is managed by another routing rule.")
    payload = {
        "name": f"TijaraGO mailbox {store.pk}",
        "enabled": True,
        "matchers": [{"type": "literal", "field": "to", "value": alias}],
        "actions": [{"type": "worker", "value": [settings.CLOUDFLARE_EMAIL_WORKER_NAME]}],
    }
    path = f"/zones/{settings.CLOUDFLARE_ZONE_ID}/email/routing/rules"
    method = "POST"
    if existing:
        method = "PUT"
        path += f"/{existing['id']}"
    result = _request(method, path, payload=payload).get("result") or {}
    return result.get("id") or (existing or {}).get("id", "")


def disable_mailbox(store):
    if settings.MAILBOX_TRANSPORT_MODE == "local":
        return
    alias = alias_for(store)
    existing = _rule_for_alias(alias)
    if not existing:
        return
    if existing.get("name") != f"TijaraGO mailbox {store.pk}":
        raise EmailRoutingError("This alias is not owned by the store mailbox.")
    _request("DELETE", f"/zones/{settings.CLOUDFLARE_ZONE_ID}/email/routing/rules/{existing['id']}")
