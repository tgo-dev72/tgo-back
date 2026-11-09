"""Design catalog — reads design-stock/manifest.json on demand.

The manifest is the single source of truth for the showroom and scaffolder.
A configured release revision pins both catalog and cloud delivery to the same
commit. Explicit local delivery always reads the working-tree manifest.

Deployed (Vercel lambda) there is no local design-stock checkout, so the
manifest is fetched from GitHub (DESIGN_STOCK_REPO) with a short in-process
TTL cache. Local dev keeps reading the file directly.
"""
import json
import time
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from store.design_access import CURRENT_DESIGN_KEYS


class DesignNotFound(Exception):
    pass


_manifest_cache: dict = {"data": None, "ts": 0.0, "source": None}


def _manifest_path() -> Path:
    return Path(settings.DESIGN_STOCK_PATH) / "manifest.json"


def _fetch_manifest_from_github() -> dict | None:
    now = time.time()
    repo = getattr(settings, "DESIGN_STOCK_REPO", "") or "EnnacherZ/design-stock"
    revision = getattr(settings, "STOREFRONT_RELEASE_REVISION", "") or "main"
    source = (repo, revision)
    if (_manifest_cache["data"] is not None and _manifest_cache["source"] == source
            and now - _manifest_cache["ts"] < 300):
        return _manifest_cache["data"]
    token = settings.STOREFRONT_SOURCE_TOKEN or settings.GITHUB_TOKEN
    url = f"https://raw.githubusercontent.com/{repo}/{quote(revision, safe='')}/manifest.json"
    try:
        req = Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError, OSError):
        return None
    _manifest_cache.update(data=payload, ts=now, source=source)
    return payload


def list_designs() -> list[dict]:
    data = None
    path = _manifest_path()
    local_mode = getattr(settings, "STOREFRONT_DEPLOYMENT_MODE", "cloud") == "local"
    if (local_mode or not getattr(settings, "STOREFRONT_RELEASE_REVISION", "")) and path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = None
    if data is None:
        data = _fetch_manifest_from_github()
    if not isinstance(data, dict):
        return []
    designs = data.get("designs", [])
    # A pinned revision may predate the current theme catalogue. Never
    # advertise or accept its retired templates for a new store; existing
    # storefront rows retain their separate legacy compatibility path.
    return [d for d in designs if isinstance(d, dict) and d.get("key") in CURRENT_DESIGN_KEYS]


def get_design(key: str) -> dict:
    for design in list_designs():
        if design.get("key") == key:
            return design
    raise DesignNotFound(key)
