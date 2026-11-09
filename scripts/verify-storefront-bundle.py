#!/usr/bin/env python3
"""Build a disposable isolated storefront, without DB or provider mutations.

Run with the backend virtualenv. npm ci may need registry access; --offline uses
the existing npm cache. Only generated temporary files and npm cache are written.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

from django.conf import settings
from storefront_factory.bundle import prepare_isolated_bundle, publishable_path
from storefront_factory.models import StorefrontRequest
from storefront_factory.scaffold import scaffold_storefront


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", default="basic")
    parser.add_argument("--source-path", default=settings.DESIGN_STOCK_PATH)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--npm-cache", default="/tmp/storefront-bundle-npm-cache")
    args = parser.parse_args()
    source = Path(args.source_path).resolve()
    available = {design["key"] for design in json.loads((source / "manifest.json").read_text())["designs"]}
    if args.design not in available:
        parser.error("Choose a supported storefront family.")
    with tempfile.TemporaryDirectory(prefix="verify-storefront-bundle-") as directory:
        scratch = Path(directory)
        for folder in (f"designs/{args.design}", "packages"):
            shutil.copytree(source / folder, scratch / folder,
                ignore=lambda _directory, names: [n for n in names if not publishable_path(n)])
        for filename in ("package.json", "package-lock.json", "THIRD_PARTY_NOTICES.md"):
            if (source / filename).is_file():
                shutil.copyfile(source / filename, scratch / filename)
        # Unsaved defaults only; no database reads or writes.
        defaults = StorefrontRequest(name="Bundle canary", slug="bundle-canary", design=args.design).__dict__
        req = SimpleNamespace(**defaults)
        req.categories = SimpleNamespace(all=lambda: [SimpleNamespace(key="general", label="General", icon="FaBoxOpen:fa")])
        req.source_repository = settings.DESIGN_STOCK_REPO
        req.target_revision = "a" * 40  # Synthetic marker, not release evidence.
        scaffold_storefront(req, repo_root=scratch, skip_subprocess=True)
        prepare_isolated_bundle(req, scratch, scratch / "output")
        bundle = scratch / "output/apps/bundle-canary"
        # Do not give npm lifecycle scripts backend/provider credentials.
        env = {"PATH": os.environ["PATH"], "VITE_API_URL": "https://api.example.invalid", "VITE_STORE_SLUG": req.slug,
               "npm_config_cache": args.npm_cache, "npm_config_audit": "false", "npm_config_fund": "false"}
        command = ["npm", "ci", "--include=dev"] + (["--offline"] if args.offline else [])
        subprocess.run(command, cwd=bundle, env=env, check=True, timeout=300)
        subprocess.run(["npm", "run", "build"], cwd=bundle, env=env, check=True, timeout=300)
        dist = bundle / f"designs/{args.design}/dist"
        marker = json.loads((dist / "_tijarago/health.json").read_text())
        if marker["store_slug"] != req.slug or marker["release_revision"] != req.target_revision:
            raise RuntimeError("Built storefront health marker does not match its input.")
        if (dist / "index.html").exists():
            raise RuntimeError("The static SPA shell would bypass the SEO renderer at the root route.")
        assets = [path for path in (dist / "assets").iterdir() if path.is_file()]
        if not any(path.suffix == ".js" for path in assets) or not any(path.suffix == ".css" for path in assets):
            raise RuntimeError("The packaged storefront is missing its static JavaScript or CSS assets.")
        module = bundle / "api/_seo-shell.mjs"
        module_text = module.read_text()
        if 'id=\\"root\\"' not in module_text or str(scratch) in module_text:
            raise RuntimeError("The packaged SEO shell is missing its app marker or leaks a build path.")
        config = json.loads((bundle / "vercel.json").read_text())
        rewrites = config["rewrites"]
        if rewrites[1]["source"] != "/robots.txt" or rewrites[2]["source"] != "/sitemap.xml":
            raise RuntimeError("SEO protocol routes no longer precede the document renderer.")

        harness = Path(__file__).with_name("test-packaged-storefront-seo-renderer.mjs")
        isolated = scratch / "isolated-function"
        shutil.copytree(bundle / "api", isolated)
        subprocess.run(["node", str(harness), str(isolated)], cwd=scratch, env=env, check=True, timeout=60)
        (isolated / "_seo-shell.mjs").write_text('export const STOREFRONT_HTML = "";\n')
        subprocess.run(["node", str(harness), str(isolated), "--expect-missing"], cwd=scratch, env=env, check=True, timeout=60)
        print(f"PASS: isolated {args.design} install, production build, packaged function execution, assets, protocol routes and missing-shell failure.")


if __name__ == "__main__":
    main()
