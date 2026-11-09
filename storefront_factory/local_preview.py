"""Opt-in local storefront delivery using the same tenant API as production."""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from django.conf import settings


def _guard():
    if settings.STOREFRONT_DEPLOYMENT_MODE != "local" or not settings.DEBUG or os.environ.get("VERCEL") == "1":
        raise RuntimeError("Local storefront delivery is disabled in this environment.")
    if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3":
        raise RuntimeError("Local storefront delivery requires the SQLite development database.")
    api = urlparse(settings.DEV_API_URL)
    if api.scheme not in {"http", "https"} or api.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("DEV_API_URL must point to the local backend in local storefront mode.")


def _pid_file(request_id):
    return Path("/tmp") / f"tijarago-storefront-preview-{request_id}.json"


def stop_local_preview(request_id):
    """Stop only the Vite process recorded for this request, if still running."""
    marker = _pid_file(request_id)
    if not marker.exists():
        return
    try:
        data = json.loads(marker.read_text())
        pid = int(data["pid"])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        if "vite/bin/vite.js" in cmdline and f"--port {int(data['port'])}" in cmdline:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    marker.unlink(missing_ok=True)


def run_local_pipeline(req, on_state=None):
    """Serve a ready design against this SQLite tenant; never call providers."""
    _guard()
    if any((req.vercel_project_id, req.vercel_deployment_id, req.github_commit_sha)) or (
        req.deployed_url and urlparse(req.deployed_url).hostname not in {"localhost", "127.0.0.1"}
    ):
        raise RuntimeError("A hosted storefront cannot be replaced by a local preview.")
    design_dir = Path(settings.DESIGN_STOCK_PATH).resolve() / "designs" / req.design
    vite = Path(settings.DESIGN_STOCK_PATH).resolve() / "node_modules/vite/bin/vite.js"
    node = shutil.which("node")
    if not design_dir.is_dir() or not (design_dir / "package.json").is_file():
        raise RuntimeError(f"Local design {req.design!r} is missing from DESIGN_STOCK_PATH.")
    if not vite.is_file() or not node:
        raise RuntimeError("Install design-stock dependencies and Node before creating a local storefront.")
    stop_local_preview(req.pk)
    # Django's development CORS/CSRF origin policy admits ports 5173-5199.
    # Stay in a dedicated range so local merchant pages can call the API.
    port = None
    for candidate in range(5190, 5200):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", candidate))
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        raise RuntimeError("All local storefront preview ports (5190-5199) are in use.")
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(PORT=str(port), VITE_API_URL=settings.DEV_API_URL, VITE_STORE_SLUG=req.slug)
    log_path = Path("/tmp") / f"tijarago-storefront-preview-{req.pk}.log"
    if on_state:
        on_state(req.PIPELINE_BUILDING, 82, "deployment.building")
    with log_path.open("ab") as output:
        process = subprocess.Popen(
            [node, str(vite), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=design_dir, env=env, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
        )
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError(f"Local Vite server exited; see {log_path}.")
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Local Vite server did not become ready; see {log_path}.")
    except Exception:
        process.terminate()
        raise
    _pid_file(req.pk).write_text(json.dumps({"pid": process.pid, "port": port}))
    req.app_path = str(design_dir)
    req.port = port
    req.domain = url
    req.deployed_url = url
    req.log = f"Local preview: {url}\nDesign: {req.design}\nAPI: {settings.DEV_API_URL}"
    req.deploy_log = req.log
    req.save(update_fields=["app_path", "port", "domain", "deployed_url", "log", "deploy_log"])
    if on_state:
        on_state(req.PIPELINE_LIVE, 100, "deployment.live")
    return req.log
