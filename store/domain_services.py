"""Transactional custom-domain lifecycle with provider and network boundaries."""
from __future__ import annotations

import ipaddress
import json
import secrets
from datetime import timedelta

import requests
import urllib3
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from storefront_factory.vercel_deploy import (
    DeployError, attach_external_domain, configure_external_domain_redirect,
    read_domain_configuration, read_project_domain, remove_external_domain,
)

from .domain_validation import normalize_storefront_hostname
from .models import StoreConfig, StorefrontDomain, StorefrontDomainEvent


class DomainConnectionError(RuntimeError):
    def __init__(self, code, public_message, internal=""):
        super().__init__(internal or public_message)
        self.code, self.public_message, self.internal = code, public_message, internal


def custom_domains_enabled():
    return bool(getattr(settings, "STOREFRONT_CUSTOM_DOMAINS_ENABLED", False))


def invalidate_domain_origin(hostname):
    cache.delete(f"storefront-domain-origin:{hostname}")


def ensure_platform_domain(store, *, active=False):
    hostname = f"{store.slug}.{settings.STOREFRONT_BASE_DOMAIN.strip('.').lower()}"
    defaults = {"display_hostname": hostname, "kind": StorefrontDomain.Kind.PLATFORM,
                "ownership_token": secrets.token_urlsafe(32)}
    domain, _ = StorefrontDomain.objects.get_or_create(
        store=store, hostname=hostname, defaults=defaults,
    )
    if active and (domain.status != StorefrontDomain.Status.ACTIVE or not StorefrontDomain.objects.filter(store=store, is_primary=True).exists()):
        with transaction.atomic():
            domain.status = StorefrontDomain.Status.ACTIVE
            domain.is_primary = not StorefrontDomain.objects.filter(store=store, is_primary=True).exists()
            domain.provider_verified = True
            domain.certificate_status = StorefrontDomain.CertificateStatus.VALID
            domain.health_verified_at = timezone.now()
            domain.save()
    return domain


def create_custom_domain(store, value):
    hostname, display = normalize_storefront_hostname(value)
    token = secrets.token_urlsafe(32)
    record = {"purpose": "ownership", "type": "TXT", "name": f"_tijarago.{hostname}",
              "value": f"tgo-domain-verification={token}"}
    try:
        with transaction.atomic():
            existing = StorefrontDomain.objects.select_for_update().filter(hostname=hostname).first()
            if existing:
                if existing.store_id != store.id:
                    raise DomainConnectionError("domain_claimed", "This domain is already connected to another store.")
                return existing, False
            domain = StorefrontDomain.objects.create(
                store=store, hostname=hostname, display_hostname=display,
                kind=StorefrontDomain.Kind.CUSTOM,
                status=StorefrontDomain.Status.AWAITING_OWNERSHIP,
                ownership_token=token, expected_dns_records=[record],
                public_message="Add the ownership TXT record, then check the connection.",
            )
            StorefrontDomainEvent.objects.create(domain=domain, event="submitted")
            return domain, True
    except IntegrityError as exc:
        raise DomainConnectionError("domain_claimed", "This domain was claimed concurrently. Refresh and try again.") from exc


def set_domain_redirect(domain, redirect_to):
    """Persist the requested companion relationship without adding schema."""
    if domain.hostname == redirect_to:
        raise DomainConnectionError("invalid_redirect", "A domain cannot redirect to itself.")
    target = StorefrontDomain.objects.filter(store=domain.store, hostname=redirect_to).first()
    if not target:
        raise DomainConnectionError("redirect_target_missing", "Connect the main domain before its companion redirect.")
    event, _ = StorefrontDomainEvent.objects.update_or_create(
        domain=domain, event="redirect_requested",
        defaults={"metadata": {"redirect_to": redirect_to}},
    )
    return event


def domain_redirect_target(domain):
    event = domain.events.filter(event="redirect_requested").order_by("-created_at", "-id").first()
    return str((event.metadata if event else {}).get("redirect_to") or "").strip().lower()


def _doh_answers(hostname, record_type):
    endpoint = getattr(settings, "STOREFRONT_DNS_RESOLVER_URL", "https://cloudflare-dns.com/dns-query")
    response = requests.get(endpoint, params={"name": hostname, "type": record_type},
                            headers={"Accept": "application/dns-json"}, timeout=8,
                            allow_redirects=False)
    if response.status_code != 200:
        raise DomainConnectionError("dns_unavailable", "DNS could not be checked right now. Try again shortly.")
    return [str(item.get("data", "")).strip().strip('"').rstrip(".")
            for item in response.json().get("Answer", []) if item.get("data")]


def _provider_records(hostname, configuration, attached):
    records = []
    for challenge in attached.get("verification", []) or []:
        if challenge.get("type") and challenge.get("domain") and challenge.get("value"):
            records.append({"purpose": "provider", "type": challenge["type"],
                            "name": challenge["domain"], "value": challenge["value"]})
    def preferred_values(raw):
        entries = raw if isinstance(raw, list) else [raw] if raw else []
        entries = sorted(entries, key=lambda item: item.get("rank", 999) if isinstance(item, dict) else 999)
        if not entries:
            return []
        value = entries[0].get("value") if isinstance(entries[0], dict) else entries[0]
        return [str(item) for item in (value if isinstance(value, list) else [value]) if item]

    # A CNAME cannot be published at a zone apex by conventional DNS
    # providers. Vercel returns both recommendations there, so prefer every
    # address in its highest-ranked IPv4 option. True subdomains can use the
    # preferred CNAME target.
    apex_name = str(attached.get("apexName") or "").rstrip(".").lower()
    if not apex_name:
        try:
            import tldextract
            extracted = tldextract.TLDExtract(
                suffix_list_urls=(), include_psl_private_domains=True, cache_dir=None,
            )(hostname)
            apex_name = ".".join(part for part in (extracted.domain, extracted.suffix) if part)
        except ImportError:
            apex_name = ""
    is_apex = hostname.rstrip(".").lower() == apex_name
    addresses = preferred_values(configuration.get("recommendedIPv4"))
    cnames = preferred_values(configuration.get("recommendedCNAME"))
    if is_apex and addresses:
        for address in addresses:
            records.append({"purpose": "routing", "type": "A", "name": hostname, "value": address})
    elif cnames:
        # CNAME recommendations are ranked alternatives, not records to publish
        # simultaneously. Use Vercel's first/current recommendation.
        records.append({"purpose": "routing", "type": "CNAME", "name": hostname, "value": cnames[0]})
    else:
        for address in addresses:
            records.append({"purpose": "routing", "type": "A", "name": hostname, "value": address})
    return records


def _routing_matches(records):
    routing = [item for item in records if item.get("purpose") == "routing"]
    if not routing:
        return False
    return any(item["value"].rstrip(".").lower() in {
        answer.lower() for answer in _doh_answers(item["name"], item["type"])
    } for item in routing)


def _public_addresses(hostname):
    answers = _doh_answers(hostname, "A") + _doh_answers(hostname, "AAAA")
    addresses = []
    for value in answers:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not address.is_global:
            raise DomainConnectionError("unsafe_dns", "The domain resolves to a private or reserved network.")
        addresses.append(str(address))
    if not addresses:
        raise DomainConnectionError("dns_pending", "The routing record is not visible yet.")
    return addresses


def _verify_health(domain, request):
    addresses = _public_addresses(domain.hostname)
    pool = urllib3.HTTPSConnectionPool(
        addresses[0], port=443, assert_hostname=domain.hostname,
        server_hostname=domain.hostname, cert_reqs="CERT_REQUIRED",
        ca_certs=requests.certs.where(), timeout=urllib3.Timeout(connect=5, read=8),
        maxsize=1, retries=False,
    )
    try:
        response = pool.request(
            "GET", "/_tijarago/health.json", headers={"Host": domain.hostname},
            redirect=False,
        )
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        raise DomainConnectionError("https_pending", "HTTPS is not ready yet.", str(exc)) from exc
    if response.status != 200:
        raise DomainConnectionError("health_pending", "The storefront health check is not ready yet.")
    try:
        marker = json.loads(response.data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DomainConnectionError("health_invalid", "The domain does not serve a valid TijaraGO storefront.") from exc
    revision = request.deployed_revision or request.last_known_good_revision
    if marker.get("store_slug") != domain.store.slug or (revision and marker.get("release_revision") != revision):
        raise DomainConnectionError("wrong_store", "The domain points to a different or outdated storefront.")


def verify_domain(domain_id):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().select_related("store").get(pk=domain_id)
        if domain.kind != StorefrontDomain.Kind.CUSTOM or domain.status == StorefrontDomain.Status.REMOVING:
            return domain
        # Verification is idempotent once the alias is healthy. In particular,
        # never move an active primary domain through a transient status: the
        # database invariant intentionally requires every primary domain to
        # remain active.
        if domain.status == StorefrontDomain.Status.ACTIVE and domain.healthy:
            return domain
        domain.status = StorefrontDomain.Status.VERIFYING
        domain.last_checked_at = timezone.now()
        domain.failure_code = domain.public_message = domain.internal_diagnostics = ""
        domain.save()
    try:
        ownership_value = f"tgo-domain-verification={domain.ownership_token}"
        if ownership_value not in _doh_answers(f"_tijarago.{domain.hostname}", "TXT"):
            raise DomainConnectionError("ownership_pending", "The ownership TXT record is not visible yet.")
        request = domain.store.storefront_requests.order_by("-created_at").first()
        if not request or not request.vercel_project_id:
            raise DomainConnectionError("storefront_not_deployed", "Deploy the storefront before connecting this domain.")
        attached = attach_external_domain(domain.hostname, request.vercel_project_id)
        configuration = read_domain_configuration(domain.hostname)
        provider = read_project_domain(domain.hostname, request.vercel_project_id) or {}
        challenges = []
        seen_challenges = set()
        for source in (provider, attached):
            for challenge in source.get("verification", []) or []:
                key = (challenge.get("type"), challenge.get("domain"), challenge.get("value"))
                if all(key) and key not in seen_challenges:
                    challenges.append(challenge)
                    seen_challenges.add(key)
        requirements = {**attached, **provider, "verification": challenges}
        records = _provider_records(domain.hostname, configuration, requirements)
        verified = bool(provider.get("verified"))
        routing_matches = _routing_matches(records)
        if not routing_matches or not verified:
            with transaction.atomic():
                current = StorefrontDomain.objects.select_for_update().get(pk=domain.pk)
                current.ownership_verified_at = current.ownership_verified_at or timezone.now()
                current.provider_project_id = request.vercel_project_id
                current.expected_dns_records = records
                current.provider_verified = verified
                current.status = StorefrontDomain.Status.AWAITING_DNS
                current.certificate_status = StorefrontDomain.CertificateStatus.PENDING
                if not routing_matches:
                    current.failure_code = "routing_pending"
                    current.public_message = "DNS routing is not visible yet. Keep the records below in place, then check again."
                else:
                    current.failure_code = "provider_pending"
                    current.public_message = (
                        "TijaraGO is waiting for secure hosting verification. "
                        "Keep the verification TXT record below in place; DNS updates can take a few minutes."
                    )
                current.next_retry_at = timezone.now() + timedelta(minutes=5)
                current.save()
                return current
        redirect_to = domain_redirect_target(domain)
        if redirect_to:
            target = StorefrontDomain.objects.filter(
                store=domain.store, hostname=redirect_to,
                status=StorefrontDomain.Status.ACTIVE,
            ).first()
            if not target or not target.healthy:
                raise DomainConnectionError(
                    "redirect_target_pending",
                    "The main domain is still being activated. Check again shortly.",
                )
            configure_external_domain_redirect(domain.hostname, request.vercel_project_id, redirect_to)
        else:
            _verify_health(domain, request)
        with transaction.atomic():
            current = StorefrontDomain.objects.select_for_update().get(pk=domain.pk)
            current.ownership_verified_at = current.ownership_verified_at or timezone.now()
            current.provider_project_id = request.vercel_project_id
            current.expected_dns_records = records
            current.provider_verified = True
            current.certificate_status = StorefrontDomain.CertificateStatus.VALID
            current.health_verified_at = timezone.now()
            current.status = StorefrontDomain.Status.ACTIVE
            current.public_message = (
                f"Redirecting permanently to {redirect_to}." if redirect_to
                else "Connected and ready to use."
            )
            current.next_retry_at = None
            current.save()
            StorefrontDomainEvent.objects.create(domain=current, event="activated")
            invalidate_domain_origin(current.hostname)
            return current
    except (DomainConnectionError, DeployError) as exc:
        error = exc if isinstance(exc, DomainConnectionError) else DomainConnectionError("provider_error", "The hosting provider could not complete this check.", str(exc))
        with transaction.atomic():
            current = StorefrontDomain.objects.select_for_update().get(pk=domain.pk)
            pending_dns_codes = {
                "dns_pending", "https_pending", "health_pending",
                "redirect_target_pending", "provider_pending",
            }
            current.status = (
                StorefrontDomain.Status.AWAITING_OWNERSHIP if error.code == "ownership_pending"
                else StorefrontDomain.Status.AWAITING_DNS if error.code in pending_dns_codes
                else StorefrontDomain.Status.FAILED
            )
            current.failure_code = error.code
            current.public_message = error.public_message
            current.internal_diagnostics = error.internal[:2000]
            current.next_retry_at = timezone.now() + timedelta(minutes=5)
            current.save()
            StorefrontDomainEvent.objects.create(domain=current, event="check_failed", public_message=error.public_message,
                                                 metadata={"code": error.code})
            return current


def make_primary(domain_id):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().select_related("store").get(pk=domain_id)
        if domain_redirect_target(domain):
            raise DomainConnectionError("redirect_cannot_be_primary", "A redirect address cannot be the primary domain.")
        if not domain.healthy:
            raise DomainConnectionError("not_healthy", "Only a fully verified, healthy domain can be primary.")
        StorefrontDomain.objects.select_for_update().filter(store=domain.store).exclude(pk=domain.pk).update(is_primary=False)
        domain.is_primary = True
        domain.save(update_fields=["is_primary", "updated_at"])
        StoreConfig.objects.filter(store=domain.store).update(domain=f"https://{domain.hostname}")
        StorefrontDomainEvent.objects.create(domain=domain, event="made_primary")
        invalidate_domain_origin(domain.hostname)
        return domain


def begin_disconnect(domain_id):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().select_related("store").get(pk=domain_id)
        if domain.kind == StorefrontDomain.Kind.PLATFORM:
            raise DomainConnectionError("platform_fallback", "The TijaraGO fallback address cannot be disconnected.")
        was_primary, hostname, project_id, store = domain.is_primary, domain.hostname, domain.provider_project_id, domain.store
        domain.status = StorefrontDomain.Status.REMOVING
        domain.is_primary = False
        domain.save(update_fields=["status", "is_primary", "updated_at"])
        if was_primary:
            fallback = ensure_platform_domain(store, active=True)
            StoreConfig.objects.filter(store=store).update(domain=f"https://{fallback.hostname}")
    invalidate_domain_origin(hostname)
    return hostname, project_id


def finalize_disconnect(domain_id):
    domain = StorefrontDomain.objects.select_related("store").get(pk=domain_id)
    hostname, project_id = domain.hostname, domain.provider_project_id
    if project_id:
        remove_external_domain(hostname, project_id)
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().get(pk=domain_id)
        domain.delete()
    invalidate_domain_origin(hostname)


def disconnect_domain(domain_id):
    """Synchronous primitive retained for maintenance and focused tests."""
    begin_disconnect(domain_id)
    finalize_disconnect(domain_id)
