"""
Scaffolds a new frontend storefront app under design-stock's apps/ directory,
by cloning the merchant's chosen design from designs/<design> (a pristine
template in the design-stock monorepo) and injecting the store-specific bits:
package name, dev-server port, dev TLS cert, and per-store config
(store.config.ts: name, domain, contact email, currency, color palette;
taxonomy.config.ts + locale productTypes from the StorefrontCategory rows
attached to the request; generated logo + banner PNGs; and .env files that
point the app at the real store-platform API with the store's own slug).

Local-dev only: this does not touch Vercel, git, or any backend product
data — it only produces a runnable apps/<slug> directory plus an
`npm install` at the design-stock root so the new workspace is linked.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings

from .models import ICON_MODULES
from .domains import default_storefront_url


def storefront_base_url(req) -> str:
    """Public base URL the deployed storefront will live at."""
    if req.domain and "CHANGE-ME" not in req.domain:
        return req.domain
    return default_storefront_url(req.slug)

STOREFRONT_SECURITY_HEADERS = [
    {"key": "Content-Security-Policy", "value": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https:; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; img-src 'self' data: blob: https:; font-src 'self' data: https://fonts.gstatic.com; connect-src 'self' https://api.tijarago.com wss://api.tijarago.com; manifest-src 'self'; worker-src 'self' blob:; upgrade-insecure-requests"},
    {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
    {"key": "X-Content-Type-Options", "value": "nosniff"},
    {"key": "X-Frame-Options", "value": "DENY"},
    {"key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=(), browsing-topics=()"},
    {"key": "Strict-Transport-Security", "value": "max-age=63072000; includeSubDomains; preload"},
    {"key": "Cross-Origin-Opener-Policy", "value": "same-origin"},
]


def storefront_security_headers(req=None):
    """Keep merchant storefronts non-frameable; narrowly allow official demos."""
    if req is None or not getattr(req, "store_id", None):
        return STOREFRONT_SECURITY_HEADERS
    try:
        from theme_previewer.models import ThemeDemoStore

        is_demo = ThemeDemoStore.objects.filter(
            store_id=req.store_id, theme_key=req.design,
        ).exists()
    except Exception:
        is_demo = False
    if not is_demo:
        return STOREFRONT_SECURITY_HEADERS
    origins = tuple(getattr(settings, "THEME_PREVIEW_ALLOWED_ORIGINS", ()))
    frame_ancestors = " ".join(origins) or "'none'"
    headers = []
    for header in STOREFRONT_SECURITY_HEADERS:
        if header["key"] == "X-Frame-Options":
            continue
        if header["key"] == "Content-Security-Policy":
            headers.append({
                **header,
                "value": header["value"].replace("frame-ancestors 'none'", f"frame-ancestors {frame_ancestors}"),
            })
        else:
            headers.append(dict(header))
    return headers


IGNORE_PATTERNS = shutil.ignore_patterns(
    "node_modules", "dist", ".vite", "*.tsbuildinfo", "key.pem", "cert.pem",
)


class ScaffoldError(RuntimeError):
    pass


def _next_free_port(apps_dir: Path) -> int:
    used = set()
    # apps/* — previously scaffolded storefronts
    for cfg in apps_dir.glob("*/vite.config.ts"):
        for match in re.findall(r"(?:port:\s*|\?\?\s*)(\d+)", cfg.read_text()):
            used.add(int(match))
    # designs/* — the template dev servers claim these ports, so a fresh
    # scaffold must never collide with a running design preview
    repo_root = apps_dir.parent
    for cfg in (repo_root / "designs").glob("*/vite.config.ts"):
        for match in re.findall(r"(?:port:\s*|\?\?\s*)(\d+)", cfg.read_text()):
            used.add(int(match))
    port = 5173
    while port in used:
        port += 1
    return port


def _available_designs(repo_root: Path) -> list[str]:
    manifest_path = repo_root / "manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text())
        return [d["key"] for d in data.get("designs", [])]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _initials(name: str) -> str:
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if not words:
        return "ST"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _make_logo(path: Path, initials: str, bg, fg):
    from PIL import Image, ImageDraw, ImageFont

    size = (300, 300)
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([10, 10, 290, 290], radius=48, fill=bg)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 120)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), initials, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size[0] - w) / 2 - bbox[0], (size[1] - h) / 2 - bbox[1]), initials, fill=fg, font=font)
    img.save(path)


def _make_banner(path: Path, label: str, bg, fg):
    from PIL import Image, ImageDraw, ImageFont

    size = (800, 450)
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    text = f"{label}"
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size[0] - w) / 2 - bbox[0], (size[1] - h) / 2 - bbox[1]), text, fill=fg, font=font)
    img.save(path)


def _build_store_config_ts(req) -> str:
    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    return (
        'import type { StoreConfig } from "@storefront/core";\n'
        "\n"
        "/**\n"
        " * Per-store configuration — generated by the Django storefront factory.\n"
        " * The factory owns this file; do not edit by hand.\n"
        " */\n"
        "export const storeConfig: StoreConfig = {\n"
        f'  storeName: "{esc(req.name)}",\n'
        f'  domain: "{esc(storefront_base_url(req))}",\n'
        f'  contactEmail: "{esc(req.contact_email or "contact@example.com")}",\n'
        f'  currency: "{esc(req.currency or "MAD")}",\n'
        "  palette: {\n"
        f'    brand: "{req.color_brand}",\n'
        f'    brandDark: "{req.color_brand_dark}",\n'
        f'    brandDeep: "{req.color_brand_deep}",\n'
        f'    ink: "{req.color_ink}",\n'
        f'    brandLight: "{req.color_brand_light}",\n'
        f'    brandLight1: "{req.color_brand_light_1}",\n'
        f'    brandLight2: "{req.color_brand_light_2}",\n'
        "  },\n"
        "};\n"
    )


def _write_env_files(target_dir, req) -> None:
    """Write the storefront's environment files.

    Modes:
      preview     — built with `vite build --mode preview`; renders the DEMO
                    API (sample catalog) so a design can be simulated before
                    launch.
      development — `npm run dev`; also the demo API for the same reason.
      production  — deployed builds; the REAL store-platform API + this
                    store's slug, so a live storefront shows real products.
    The root `.env` is read by the Vercel deploy step (_set_env_vars), which
    uploads every VITE_* value as build-time env vars.
    """
    slug = req.store.slug if req.store_id else req.slug
    production_url = getattr(settings, "STOREFRONT_API_URL", "")
    dev_url = getattr(settings, "DEV_API_URL", "") or production_url
    demo_url = getattr(settings, "DEMO_API_URL", "") or production_url
    header = "# Generated by the Django storefront factory.\n"

    (target_dir / ".env").write_text(
        header + f"VITE_API_URL={production_url}\nVITE_STORE_SLUG={slug}\n"
    )
    (target_dir / ".env.production").write_text(
        header + f"VITE_API_URL={production_url}\nVITE_STORE_SLUG={slug}\n"
    )
    (target_dir / ".env.preview").write_text(
        header + f"VITE_API_URL={demo_url}\nVITE_STORE_SLUG={slug}\n"
    )
    (target_dir / ".env.development").write_text(
        header + f"VITE_API_URL={dev_url}\nVITE_STORE_SLUG={slug}\n"
    )


def _build_taxonomy_ts(categories) -> str:
    icon_imports: dict[str, set[str]] = {}
    banner_import_lines = []
    taxonomy_lines = []
    title_lines = []
    banner_lines = []
    icon_lines = []

    for cat in categories:
        icon_name, mod = cat.icon.split(":")
        icon_imports.setdefault(mod, set()).add(icon_name)
        banner_var = f"banner_{cat.key.replace('-', '_')}"
        banner_import_lines.append(
            f'import {banner_var} from "../assets/banner-{cat.key}-PLACEHOLDER.png";'
        )
        escaped_label = cat.label.replace("\\", "\\\\").replace('"', '\\"')
        taxonomy_lines.append(
            f'  {{ key: "{cat.key}", label: "{escaped_label}", '
            f'icon: "{cat.icon}", banner: {banner_var} }},'
        )
        key_literal = json.dumps(cat.key)
        title_lines.append(f'  {key_literal}: "{escaped_label}",')
        banner_lines.append(f"  {key_literal}: {banner_var},")
        icon_lines.append(f"  {key_literal}: {icon_name},")

    icon_import_lines = [
        f'import {{ {", ".join(sorted(names))} }} from "{ICON_MODULES[mod]}";'
        for mod, names in icon_imports.items()
    ]

    return (
        "/**\n"
        " * taxonomy.config.ts — auto-generated by the Django storefront factory.\n"
        " * Section keys must match the backend catalog's section slugs exactly.\n"
        " */\n"
        'import React from "react";\n'
        'import type { TaxonomyCategory } from "@storefront/core";\n'
        + "\n".join(icon_import_lines) + "\n"
        + "\n".join(banner_import_lines) + "\n\n"
        "export const taxonomy: TaxonomyCategory[] = [\n"
        + "\n".join(taxonomy_lines) + "\n"
        "];\n\n"
        "export const productTitle: Record<string, string> = {\n"
        + "\n".join(title_lines) + "\n"
        "};\n\n"
        "export const productBanner: Record<string, string> = {\n"
        + "\n".join(banner_lines) + "\n"
        "};\n\n"
        "export const productIcon: Record<string, React.ElementType> = {\n"
        + "\n".join(icon_lines) + "\n"
        "};\n"
    )


def _rewrite_locale_product_types(text: str, categories) -> str:
    block_lines = ["productTypes: {"]
    block_lines.append(
        "  // Generated from the store's catalog sections; edit these labels per language before launch."
    )
    for cat in categories:
        escaped_label = cat.label.replace("\\", "\\\\").replace("'", "\\'")
        block_lines.append(f"  {json.dumps(cat.key)}: '{escaped_label}',")
    block_lines.append("}")
    new_block = "\n".join(block_lines)
    return re.sub(r"productTypes: \{[^}]*\}", new_block, text, count=1, flags=re.DOTALL)


def scaffold_storefront(req, repo_root=None, skip_subprocess=False) -> str:
    """Runs the full scaffold for a StorefrontRequest. Returns a log string on
    success. Raises ScaffoldError on a failure that leaves nothing usable.

    repo_root overrides settings.DESIGN_STOCK_PATH (used by the in-cloud
    pipeline, which fetches the selected design into the function's writable
    /tmp instead of requiring a local design-stock checkout). With skip_subprocess=True
    the openssl cert + npm install steps are skipped entirely — both are
    local-dev niceties, not requirements for a Vercel build.
    """
    log_lines: list[str] = []

    def log(msg: str):
        log_lines.append(msg)

    repo_root = Path(repo_root) if repo_root else Path(settings.DESIGN_STOCK_PATH)
    apps_dir = repo_root / "apps"
    template_dir = repo_root / "designs" / req.design
    target_dir = apps_dir / req.slug

    if not template_dir.is_dir():
        available = ", ".join(_available_designs(repo_root)) or "none"
        raise ScaffoldError(
            f"Design '{req.design}' not found at {template_dir} "
            f"— available designs in design-stock: {available}"
        )
    if target_dir.exists():
        raise ScaffoldError(f"apps/{req.slug} already exists — pick a different slug or remove it first")

    categories = list(req.categories.all())
    if not categories:
        raise ScaffoldError("Add at least one category before scaffolding")

    apps_dir.mkdir(parents=True, exist_ok=True)

    port = req.port or _next_free_port(apps_dir)
    log(f"Assigned dev server port {port}")

    shutil.copytree(template_dir, target_dir, ignore=IGNORE_PATTERNS)
    log(f"Copied designs/{req.design} → apps/{req.slug}")

    # A failure at any point below must not leave a half-written apps/<slug>
    # behind — otherwise a retry dies on the "already exists" guard with a
    # partial, non-runnable directory stuck in the repo.
    try:
        # package.json — rename either basic (@firdaous/*) or premium
        # (@platform/*) templates to the merchant app's unique workspace name.
        # Parsing JSON is more reliable than assuming a particular package scope.
        pkg_path = target_dir / "package.json"
        package_data = json.loads(pkg_path.read_text())
        if not isinstance(package_data, dict):
            raise ValueError("package.json must contain a JSON object")
        package_data["name"] = f"@firdaous/{req.slug}"
        pkg_path.write_text(json.dumps(package_data, indent=2) + "\n")
        log("Updated package.json name")
    
        # vite.config.ts — dev port (default is `Number(env.PORT ?? <port>)`)
        vite_path = target_dir / "vite.config.ts"
        vite_path.write_text(re.sub(r"\?\?\s*\d+", f"?? {port}", vite_path.read_text()))
        log(f"Set vite.config.ts dev port to {port}")
    
        # fresh self-signed dev cert (never copy the template's — see IGNORE_PATTERNS)
        if not skip_subprocess:
            try:
                subprocess.run(
                    [
                        "openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-keyout", "key.pem", "-out", "cert.pem",
                        "-days", "365", "-nodes", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                    ],
                    cwd=target_dir, check=True, capture_output=True, timeout=30,
                )
                log("Generated self-signed dev TLS cert (key.pem / cert.pem)")
            except Exception as exc:  # noqa: BLE001 — report and continue, this isn't fatal
                log(f"WARNING: cert generation failed ({exc}) — generate key.pem/cert.pem manually to use HTTPS locally")
        else:
            log("Skipped dev TLS cert generation (cloud run)")
    
        # Older templates imported generated source configuration. Current
        # templates use bootStorefront(), which loads identity, palette and
        # catalog sections from /api/storefront/config/ at runtime and therefore
        # intentionally do not contain src/config or src/locales. Keep the
        # legacy generator only when the copied template declares that layout.
        store_cfg_path = target_dir / "src/config/store.config.ts"
        taxonomy_path = target_dir / "src/config/taxonomy.config.ts"
        uses_legacy_source_config = store_cfg_path.is_file() or taxonomy_path.is_file()
        if uses_legacy_source_config:
            store_cfg_path.parent.mkdir(parents=True, exist_ok=True)
            store_cfg_path.write_text(_build_store_config_ts(req))
            taxonomy_path.write_text(_build_taxonomy_ts(categories))
            log("Generated legacy source configuration (store identity + catalog taxonomy)")

            rewritten_locales = []
            for lang in ("en", "fr", "ar"):
                locale_path = target_dir / f"src/locales/{lang}.ts"
                if locale_path.is_file():
                    locale_path.write_text(
                        _rewrite_locale_product_types(locale_path.read_text(), categories)
                    )
                    rewritten_locales.append(lang)
            if rewritten_locales:
                log(f"Rewrote productTypes in {', '.join(rewritten_locales)} locale files")
        else:
            log("Using runtime store configuration (no generated src/config files required)")

        # per-store API wiring — real backend + this store's slug
        _write_env_files(target_dir, req)
        log("Wrote .env / .env.development / .env.production (real API base + store slug)")

        # Legacy templates reference generated placeholder assets. Runtime-
        # configured templates own their visual assets and need no generated
        # src/assets directory.
        assets_dir = target_dir / "src/assets"
        placeholder_assets = list(assets_dir.glob("*-PLACEHOLDER.png"))
        if uses_legacy_source_config or placeholder_assets:
            assets_dir.mkdir(parents=True, exist_ok=True)
            for asset in placeholder_assets:
                asset.unlink()
            initials = _initials(req.name)
            _make_logo(assets_dir / "white-logo-PLACEHOLDER.png", initials, bg=req.color_brand, fg="#ffffff")
            _make_logo(assets_dir / "standard-logo-PLACEHOLDER.png", initials, bg="#ffffff", fg=req.color_brand)
            _make_logo(assets_dir / "black-logo-PLACEHOLDER.png", initials, bg="#000000", fg="#ffffff")
            for cat in categories:
                _make_banner(
                    assets_dir / f"banner-{cat.key}-PLACEHOLDER.png",
                    cat.label,
                    bg=req.color_brand_light_1,
                    fg=req.color_ink,
                )
            log(f"Generated {3 + len(categories)} legacy placeholder assets")
        else:
            log("Kept template-owned visual assets")

        # index.html — title/description
        index_html_path = target_dir / "index.html"
        text = re.sub(r"<title>[^<]*</title>", f"<title>{req.name}</title>", index_html_path.read_text())
        index_html_path.write_text(text)
        log("Updated index.html title")

        # vercel.json — the design-stock repo's ROOT vercel.json forces
        # `npm run build:storefront` on every connected project; a vercel.json
        # inside the app itself overrides that (the same fix the showroom uses).
        renderer_source = Path(__file__).with_name("seo_renderer.mjs").read_text()
        shell_builder_source = Path(__file__).with_name("prepare_seo_shell.mjs").read_text()
        renderer_dir = target_dir / "api"
        renderer_dir.mkdir(exist_ok=True)
        (renderer_dir / "seo-render.mjs").write_text(renderer_source)
        (renderer_dir / "_seo-shell.mjs").write_text('export const STOREFRONT_HTML = "";\n')
        scripts_dir = target_dir / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        (scripts_dir / "prepare-seo-shell.mjs").write_text(shell_builder_source)
        (target_dir / "vercel.json").write_text(
            json.dumps({
                "buildCommand": "npm run build && node scripts/prepare-seo-shell.mjs",
                "outputDirectory": "dist",
                "framework": "vite",
                "headers": [
                    {"source": "/(.*)", "headers": storefront_security_headers(req)},
                    {"source": "/account", "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow, noarchive"}]},
                ],
                "rewrites": [
                    {
                        "source": "/_tijarago/health.json",
                        "destination": "/_tijarago/health.json",
                    },
                    {
                        "source": "/robots.txt",
                        "destination": f"{settings.STOREFRONT_API_URL.rstrip('/')}/api/public/storefront/{req.slug}/robots.txt",
                    },
                    {
                        "source": "/sitemap.xml",
                        "destination": f"{settings.STOREFRONT_API_URL.rstrip('/')}/api/public/storefront/{req.slug}/sitemap.xml",
                    },
                    {"source": "/(.*)", "destination": "/api/seo-render?path=/$1"},
                ],
            }, indent=2) + "\n"
        )
        log("Wrote Vercel SEO renderer, build command, and tenant document rewrites")

        # This immutable marker lets the backend verify the exact storefront,
        # rather than mistaking a generic 200 page, redirect, or provider error
        # document for a successful release.
        health_dir = target_dir / "public" / "_tijarago"
        health_dir.mkdir(parents=True, exist_ok=True)
        (health_dir / "health.json").write_text(
            json.dumps(
                {
                    "store_slug": req.slug,
                    "design": req.design,
                    "release_revision": req.target_revision,
                },
                separators=(",", ":"),
            ) + "\n"
        )
        log("Wrote storefront identity health marker")
    
        req.app_path = str(target_dir)
        req.port = port

    except Exception as exc:  # noqa: BLE001 — clean up the partial dir, then fail loudly
        shutil.rmtree(target_dir, ignore_errors=True)
        raise ScaffoldError(
            f"Scaffold failed and apps/{req.slug} was removed (a partial, "
            f"non-runnable copy is worse than none — you can retry now): {exc}"
        ) from exc

    # npm install so the new workspace is actually linked (best-effort — the
    # files above are already valid on disk even if this step fails). Skipped
    # entirely on cloud runs — Vercel installs deps from apps/<slug> itself.
    if skip_subprocess:
        log("Skipped npm install (cloud run — Vercel installs from apps/<slug>)")
    else:
        try:
            result = subprocess.run(
                ["npm", "install"], cwd=repo_root, capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                log("npm install completed — workspace linked")
            else:
                log(f"WARNING: npm install exited {result.returncode}:\n{result.stderr[-2000:]}")
        except Exception as exc:  # noqa: BLE001
            log(f"WARNING: could not run npm install automatically ({exc}) — run it manually from the repo root")

    log(f"Done. cd apps/{req.slug} or run: npm run dev -w @firdaous/{req.slug}  (from the design-stock root)")
    return "\n".join(log_lines)
