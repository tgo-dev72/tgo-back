"""Vercel/DNS adapters using each request's persisted deployment destination."""
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from .domains import default_storefront_url

VERCEL_API = "https://api.vercel.com"


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentResult:
    url: str
    deployment_id: str
    ready_state: str


def get_deployment_state(deployment_id: str) -> str:
    """Return Vercel's current state for one concrete deployment."""
    response = _vercel_read(
        f"{VERCEL_API}/v13/deployments/{deployment_id}",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if response.status_code != 200:
        raise DeployError(
            f"Could not read Vercel deployment status ({response.status_code})."
        )
    return str(response.json().get("readyState", "QUEUED")).upper()


def _vercel_headers():
    return {
        "Authorization": f"Bearer {settings.VERCEL_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _vercel_params():
    return {"teamId": settings.VERCEL_TEAM_ID} if settings.VERCEL_TEAM_ID else {}


def _vercel_read(url, **kwargs):
    """Perform an idempotent Vercel read with bounded transient retries.

    Provider TLS handshakes, rate limits and 5xx responses are temporary and
    must not permanently fail a storefront on their first occurrence.
    """
    attempts = getattr(settings, "VERCEL_API_READ_ATTEMPTS", 3)
    timeout = getattr(settings, "VERCEL_API_TIMEOUT", 30)
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_error = DeployError(
                f"Vercel temporarily returned HTTP {response.status_code}."
            )
        if attempt < attempts:
            time.sleep(attempt)
    raise DeployError(
        f"Vercel could not be reached after {attempts} attempts. The operation can be retried safely."
    ) from last_error


def _get_project(name):
    resp = _vercel_read(
        f"{VERCEL_API}/v9/projects/{name}",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    raise DeployError(f"Vercel GET project failed ({resp.status_code}): {resp.text}")


def remove_vercel_project(project_id_or_name):
    """Delete one Vercel project; a missing project is already clean."""
    if not settings.VERCEL_API_TOKEN:
        raise DeployError("VERCEL_API_TOKEN is not set — cannot remove the Vercel project.")
    resp = requests.delete(
        f"{VERCEL_API}/v9/projects/{project_id_or_name}",
        headers=_vercel_headers(), params=_vercel_params(), timeout=30,
    )
    if resp.status_code in (200, 204):
        return True
    if resp.status_code == 404:
        return False
    raise DeployError(
        f"Vercel project deletion failed ({resp.status_code}): {resp.text[:400]}"
    )


def _create_project(req, log):
    from .repositories import destination
    repository, _, app_path = destination(req)
    body = {
        "name": req.slug,
        "framework": "vite",
        "rootDirectory": app_path,
        "gitRepository": {"type": "github", "repo": repository},
    }
    resp = requests.post(
        f"{VERCEL_API}/v10/projects",
        headers=_vercel_headers(), params=_vercel_params(), json=body, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise DeployError(f"Vercel project creation failed ({resp.status_code}): {resp.text}")
    log(f"Created Vercel project '{req.slug}' rooted at apps/{req.slug}")
    return resp.json()


def _validate_reused_project(req, project):
    from .repositories import destination
    repository, _, expected_root = destination(req)
    actual_root = project.get("rootDirectory")
    if actual_root != expected_root:
        raise DeployError(
            f"Vercel project '{req.slug}' points to {actual_root}, not {expected_root}."
        )
    link = project.get("link") or {}
    linked_repo = link.get("repo") or link.get("repoName")
    if not linked_repo or str(linked_repo).lower() not in {
        repository.lower(),
        repository.split("/")[-1].lower(),
    }:
        raise DeployError(f"Vercel project '{req.slug}' is linked to another repository.")
    linked_owner = link.get("org")
    if linked_owner and str(linked_owner).lower() != repository.split("/")[0].lower():
        raise DeployError(f"Vercel project '{req.slug}' is linked to another repository owner.")


def _ensure_project_env_vars(project_id, values, log):
    """Create or update build variables; never silently retain stale values."""
    listing = _vercel_read(
        f"{VERCEL_API}/v9/projects/{project_id}/env",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if listing.status_code != 200:
        raise DeployError(
            f"Could not inspect Vercel environment variables ({listing.status_code})."
        )
    existing = {
        item.get("key"): item
        for item in (listing.json().get("envs") or [])
        if item.get("key")
    }
    body_base = {"type": "plain", "target": ["production", "preview", "development"]}
    for key, value in values.items():
        item = existing.get(key)
        if item:
            response = requests.patch(
                f"{VERCEL_API}/v9/projects/{project_id}/env/{item['id']}",
                headers=_vercel_headers(), params=_vercel_params(),
                json={"key": key, "value": value, **body_base}, timeout=30,
            )
        else:
            response = requests.post(
                f"{VERCEL_API}/v10/projects/{project_id}/env",
                headers=_vercel_headers(), params=_vercel_params(),
                json={"key": key, "value": value, **body_base}, timeout=30,
            )
        if response.status_code not in (200, 201):
            raise DeployError(
                f"Could not configure Vercel environment variable {key} ({response.status_code})."
            )
        log(f"Configured env var {key}")


def _trigger_deployment(req, project, branch, log, on_created=None):
    """Explicitly deploy the recorded commit. Isolated bundles disable Git
    auto-deployments so a shared-repo push cannot race this owned operation."""
    repo_id = (project.get("link") or {}).get("repoId")
    if not repo_id:
        raise DeployError(f"Vercel project '{req.slug}' has no linked repoId — cannot trigger a deployment.")

    body = {
        "name": req.slug,
        "project": project["id"],
        "target": "production",
        "gitSource": {"type": "github", "repoId": repo_id, "ref": branch},
    }
    if getattr(req, "github_commit_sha", ""):
        body["gitSource"]["sha"] = req.github_commit_sha
    resp = requests.post(
        f"{VERCEL_API}/v13/deployments",
        headers=_vercel_headers(), params=_vercel_params(), json=body, timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise DeployError(f"Vercel deployment trigger failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    deployment_id = data["id"]
    url = f"https://{data['url']}"
    if on_created:
        on_created(deployment_id, url)
    log(f"Triggered production deployment {deployment_id} ({url}) from {branch}")

    # Bounded poll so the admin action returns in reasonable time — build can
    # legitimately still be running after this, that's reported, not an error.
    state = data.get("readyState", "QUEUED")
    for _ in range(10):
        if state in ("READY", "ERROR", "CANCELED"):
            break
        time.sleep(4)
        state = get_deployment_state(deployment_id)

    if state == "READY":
        log("Deployment is READY.")
    elif state in ("ERROR", "CANCELED"):
        raise DeployError(f"Vercel build failed for deployment {deployment_id} — check the Vercel dashboard for build logs.")
    else:
        log(f"Deployment still {state} after polling — check the Vercel dashboard for progress.")

    return DeploymentResult(url=url, deployment_id=deployment_id, ready_state=state)


CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
# Vercel's classic, widely-peered shared anycast target. Domain-specific
# CNAME targets Vercel suggests (the "vercel-dns-017.com"-style values) have
# been observed to hit regional ISP routing gaps that this one doesn't —
# see the 2026-08-01 alfirdaousstore.com incident. Point new records here.
VERCEL_CNAME_TARGET = "cname.vercel-dns.com"


def _resolve_domain(req, log):
    """Returns the hostname this storefront should use, auto-deriving
    <slug>.STOREFRONT_BASE_DOMAIN if the request was left at the default
    placeholder, and persisting that choice onto req.domain."""
    # Store code deployments always retain their platform-owned fallback.
    # Merchant aliases are attached separately after ownership verification;
    # an old StorefrontRequest.domain value must never bypass that workflow.
    if getattr(req, "store_id", None):
        from urllib.parse import urlparse
        managed = default_storefront_url(req.slug)
        if req.domain != managed:
            req.domain = managed
            req.save(update_fields=["domain"])
        return urlparse(managed).hostname
    if req.domain and "CHANGE-ME" not in req.domain:
        from urllib.parse import urlparse
        return urlparse(req.domain).hostname or req.domain

    from urllib.parse import urlparse
    req.domain = default_storefront_url(req.slug)
    hostname = urlparse(req.domain).hostname
    log(f"No domain set — defaulting to {hostname}")
    return hostname


def _verify_provider_domain(hostname, project_id, log):
    """Require the public alias to belong to this project; never claim another."""
    response = _vercel_read(
        f"{VERCEL_API}/v9/projects/{project_id}/domains/{hostname}",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if response.status_code != 200 or response.json().get("name") != hostname:
        raise DeployError("The expected public Vercel address is not assigned to this storefront project.")
    log(f"Using the project's public Vercel address: {hostname}")


def _cloudflare_headers():
    return {"Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}", "Content-Type": "application/json"}


def _cloudflare_zone_id(log):
    if settings.CLOUDFLARE_ZONE_ID:
        return settings.CLOUDFLARE_ZONE_ID
    resp = requests.get(
        f"{CLOUDFLARE_API}/zones", headers=_cloudflare_headers(),
        params={"name": settings.STOREFRONT_BASE_DOMAIN}, timeout=30,
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise DeployError("Cloudflare zone lookup returned an invalid response.") from exc
    result = data.get("result") or []
    if resp.status_code >= 400 or not data.get("success", False) or not result:
        raise DeployError(f"Cloudflare zone lookup for {settings.STOREFRONT_BASE_DOMAIN} failed: {resp.text}")
    return result[0]["id"]


def _ensure_dns_record(hostname, log):
    """Ensure the exact platform-owned DNS record or fail deterministically."""
    base = settings.STOREFRONT_BASE_DOMAIN.strip(".").lower()
    if not hostname.rstrip(".").lower().endswith(f".{base}"):
        raise DeployError("External DNS is merchant-managed and cannot be changed by TijaraGO.")
    if not settings.CLOUDFLARE_API_TOKEN:
        raise DeployError("CLOUDFLARE_API_TOKEN is not set.")

    zone_id = _cloudflare_zone_id(log)
    lookup = requests.get(
        f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records",
        headers=_cloudflare_headers(), params={"type": "CNAME", "name": hostname}, timeout=30,
    )
    try:
        lookup_data = lookup.json()
    except ValueError as exc:
        raise DeployError("Cloudflare DNS lookup returned an invalid response.") from exc
    if lookup.status_code >= 400 or not lookup_data.get("success", False):
        raise DeployError(f"Cloudflare DNS lookup failed ({lookup.status_code}): {lookup.text[:400]}")
    existing = lookup_data.get("result") or []
    if existing:
        record = existing[0]
        target = str(record.get("content", "")).rstrip(".").lower()
        if target != VERCEL_CNAME_TARGET or bool(record.get("proxied", False)):
            raise DeployError(
                f"DNS record for {hostname} exists but is not the required DNS-only CNAME."
            )
        log(f"DNS record for {hostname} already has the required target.")
        return str(record.get("id", ""))

    resp = requests.post(
        f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records",
        headers=_cloudflare_headers(),
        json={"type": "CNAME", "name": hostname, "content": VERCEL_CNAME_TARGET, "proxied": False, "ttl": 1},
        timeout=30,
    )
    response_data = resp.json()
    if response_data.get("success"):
        log(f"Created DNS record: {hostname} CNAME {VERCEL_CNAME_TARGET} (DNS only)")
        return str((response_data.get("result") or {}).get("id", ""))
    raise DeployError(f"Cloudflare DNS record creation failed for {hostname}: {resp.text[:400]}")


def remove_cloudflare_dns_record(hostname):
    """Delete the exact platform-managed CNAME; absence is idempotent."""
    base = settings.STOREFRONT_BASE_DOMAIN.strip(".").lower()
    if not hostname.rstrip(".").lower().endswith(f".{base}"):
        return 0
    if not settings.CLOUDFLARE_API_TOKEN:
        raise DeployError("CLOUDFLARE_API_TOKEN is not set — cannot remove the DNS record.")

    zone_id = _cloudflare_zone_id(lambda _message: None)
    lookup = requests.get(
        f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records",
        headers=_cloudflare_headers(),
        params={"type": "CNAME", "name": hostname},
        timeout=30,
    )
    try:
        lookup_data = lookup.json()
    except ValueError as exc:
        raise DeployError(
            f"Cloudflare DNS lookup failed ({lookup.status_code}): invalid response."
        ) from exc
    if lookup.status_code >= 400 or not lookup_data.get("success", False):
        raise DeployError(
            f"Cloudflare DNS lookup failed ({lookup.status_code}): {lookup.text[:400]}"
        )

    records = [
        record
        for record in (lookup_data.get("result") or [])
        if record.get("type") == "CNAME"
        and str(record.get("name", "")).rstrip(".").lower() == hostname.rstrip(".").lower()
    ]
    for record in records:
        response = requests.delete(
            f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records/{record['id']}",
            headers=_cloudflare_headers(),
            timeout=30,
        )
        try:
            response_data = response.json()
        except ValueError as exc:
            raise DeployError(
                f"Cloudflare DNS deletion failed ({response.status_code}): invalid response."
            ) from exc
        if response.status_code >= 400 or not response_data.get("success", False):
            raise DeployError(
                f"Cloudflare DNS deletion failed ({response.status_code}): {response.text[:400]}"
            )
    return len(records)


def _attach_domain(hostname, project_id, log):
    resp = requests.post(
        f"{VERCEL_API}/v10/projects/{project_id}/domains",
        headers=_vercel_headers(), params=_vercel_params(),
        json={"name": hostname}, timeout=30,
    )
    if resp.status_code in (200, 201):
        log(f"Attached domain {hostname} to the Vercel project")
        return
    if resp.status_code == 409:
        verification = _vercel_read(
            f"{VERCEL_API}/v9/projects/{project_id}/domains/{hostname}",
            headers=_vercel_headers(), params=_vercel_params(),
        )
        if verification.status_code == 200:
            log(f"Domain {hostname} is already attached to this Vercel project")
            return
    raise DeployError(f"Could not attach domain {hostname} ({resp.status_code}): {resp.text[:400]}")


def read_project_domain(hostname, project_id):
    """Read one exact alias and Vercel's current verification instructions."""
    response = _vercel_read(
        f"{VERCEL_API}/v9/projects/{project_id}/domains/{hostname}",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise DeployError(f"Could not inspect domain ({response.status_code}).")
    return response.json()


def attach_external_domain(hostname, project_id):
    """Attach after ownership proof; a conflict is accepted only on this project."""
    _attach_domain(hostname, project_id, lambda _message: None)
    attached = read_project_domain(hostname, project_id)
    if not attached or attached.get("name") != hostname:
        raise DeployError("The provider did not confirm this domain on the expected project.")
    return attached


def read_domain_configuration(hostname):
    response = _vercel_read(
        f"{VERCEL_API}/v6/domains/{hostname}/config",
        headers=_vercel_headers(), params=_vercel_params(),
    )
    if response.status_code != 200:
        raise DeployError(f"Could not read provider DNS requirements ({response.status_code}).")
    return response.json()


def configure_external_domain_redirect(hostname, project_id, redirect_to):
    """Turn an attached companion hostname into a permanent canonical redirect."""
    response = requests.patch(
        f"{VERCEL_API}/v9/projects/{project_id}/domains/{hostname}",
        headers=_vercel_headers(), params=_vercel_params(), timeout=30,
        json={"redirect": redirect_to, "redirectStatusCode": 308},
    )
    if response.status_code != 200:
        raise DeployError(f"Could not configure domain redirect ({response.status_code}): {response.text[:300]}")
    return response.json()


def remove_external_domain(hostname, project_id):
    response = requests.delete(
        f"{VERCEL_API}/v9/projects/{project_id}/domains/{hostname}",
        headers=_vercel_headers(), params=_vercel_params(), timeout=30,
    )
    if response.status_code in (200, 204, 404):
        return response.status_code != 404
    raise DeployError(f"Could not detach domain ({response.status_code}): {response.text[:300]}")


def deploy_storefront(req) -> str:
    """Local callers use the same pinned pipeline; never push a developer branch."""
    from .cloud import run_pipeline
    return run_pipeline(req)
