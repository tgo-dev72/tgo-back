"""Build a store-owned, revision-pinned workspace without rewriting imports."""
import json
import shutil
from pathlib import Path, PurePosixPath

from .scaffold import ScaffoldError


DEPENDENCY_KEYS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def project_lockfile(lock, manifest, workspaces):
    """Keep the transitive installed graph using npm's ancestor resolution.

    Do not resolve versions or fetch metadata here: every selected entry comes
    verbatim from the approved lock. npm ci is the final consistency check.
    """
    records = lock["packages"]
    kept = {"": {key: manifest[key] for key in ("name", "version", "workspaces", "engines") if key in manifest}}
    pending = list(workspaces)
    for path, record in records.items():
        if record.get("link") and record.get("resolved") in workspaces:
            pending.append(path)

    def resolve(parent, name):
        ancestor = PurePosixPath(parent)
        while True:
            candidate = (ancestor / "node_modules" / name).as_posix()
            if candidate in records:
                return candidate
            if str(ancestor) == ".":
                return None
            ancestor = ancestor.parent

    while pending:
        path = pending.pop()
        if path in kept:
            continue
        if path not in records:
            raise ScaffoldError(f"The approved lockfile is missing workspace {path}.")
        record = records[path]
        kept[path] = record
        if record.get("link"):
            resolved = record.get("resolved")
            if resolved not in workspaces:
                raise ScaffoldError("A storefront dependency points to an excluded workspace.")
            pending.append(resolved)
            continue
        for key in DEPENDENCY_KEYS:
            if key == "devDependencies" and path not in workspaces:
                continue
            for name in record.get(key, {}):
                dependency = resolve(path, name)
                if dependency:
                    pending.append(dependency)
                elif key not in {"optionalDependencies", "peerDependencies"}:
                    raise ScaffoldError(f"The approved lockfile cannot resolve dependency {name}.")
    return {"name": manifest["name"], "version": manifest["version"],
            "lockfileVersion": 3, "requires": True, "packages": kept}


def publishable_path(path):
    parts = PurePosixPath(path).parts
    return not any(
        part in {"node_modules", "dist", ".git", ".vercel", "__pycache__", ".npmrc"}
        or part.startswith(".env") or part.endswith((".pem", ".key", ".tsbuildinfo"))
        for part in parts
    )


def prepare_isolated_bundle(req, source_root, output_root):
    """Preserve designs/<design> → ../../packages imports inside apps/<slug>.

    Use the source lockfile's exact registry resolutions for the selected
    workspace graph, excluding dashboard/admin tooling.
    npm ci subsequently validates that the projected lock is consistent.
    """
    source_root, output_root = Path(source_root), Path(output_root)
    generated = source_root / "apps" / req.slug
    target = output_root / "apps" / req.slug
    design_path = f"designs/{req.design}"
    try:
        manifest = json.loads((source_root / "package.json").read_text())
        lock = json.loads((source_root / "package-lock.json").read_text())
        original_design = json.loads((source_root / design_path / "package.json").read_text())
    except (OSError, ValueError) as exc:
        raise ScaffoldError("The approved release must contain package.json and an npm workspace lockfile.") from exc
    if lock.get("lockfileVersion", 0) < 2 or not isinstance(lock.get("packages"), dict):
        raise ScaffoldError("The approved release requires an npm v2/v3 lockfile.")
    available_packages = {}
    for path in (source_root / "packages").glob("*/package.json"):
        package = json.loads(path.read_text())
        available_packages[package["name"]] = (path.parent.relative_to(source_root).as_posix(), package)
    package_paths = []
    pending = [original_design]
    while pending:
        package = pending.pop()
        for key in DEPENDENCY_KEYS:
            for name in package.get(key, {}):
                if name in available_packages:
                    path, dependency = available_packages[name]
                    if path not in package_paths:
                        package_paths.append(path)
                        pending.append(dependency)
    package_paths.sort()
    if "packages/storefront-core" not in package_paths:
        raise ScaffoldError("The approved release is missing the shared storefront core.")
    workspaces = [design_path, *package_paths]

    def copy_tree(src, dst):
        for path in src.rglob("*"):
            relative = path.relative_to(src)
            if not publishable_path(relative.as_posix()):
                continue
            if path.is_symlink():
                raise ScaffoldError("Symlinks are not permitted in storefront delivery bundles.")
            if path.is_file():
                destination = dst / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)

    copy_tree(generated, target / design_path)
    for package_path in package_paths:
        copy_tree(source_root / package_path, target / package_path)
    for name in ("LICENSE", "LICENSE.md", "THIRD_PARTY_NOTICES.md"):
        if (source_root / name).is_file():
            shutil.copyfile(source_root / name, target / name)

    # Keep the original workspace name so all lockfile links remain valid.
    app_manifest_path = target / design_path / "package.json"
    app_manifest = json.loads(app_manifest_path.read_text())
    app_manifest["name"] = original_design["name"]
    app_manifest_path.write_text(json.dumps(app_manifest, indent=2) + "\n")
    manifest = {
        "name": "storefront-bundle", "version": "1.0.0", "private": True,
        "engines": manifest.get("engines", {"node": ">=20"}), "workspaces": workspaces,
        "scripts": {"build": f"npm --prefix {design_path} run build --workspaces=false && node scripts/prepare-seo-shell.mjs"},
    }
    (target / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")

    lock = project_lockfile(lock, manifest, workspaces)
    (target / "package-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    vercel = json.loads((generated / "vercel.json").read_text())
    renderer = generated / "api/seo-render.mjs"
    if renderer.is_file():
        root_renderer = target / "api/seo-render.mjs"
        root_renderer.parent.mkdir(parents=True, exist_ok=True)
        root_renderer.write_text(renderer.read_text())
        shutil.copyfile(generated / "api" / "_seo-shell.mjs", target / "api" / "_seo-shell.mjs")
        root_scripts = target / "scripts"
        root_scripts.mkdir(exist_ok=True)
        shell_builder = (generated / "scripts" / "prepare-seo-shell.mjs").read_text()
        (root_scripts / "prepare-seo-shell.mjs").write_text(
            shell_builder.replace('const APP_PATH = ".";', f'const APP_PATH = "{design_path}";')
        )
        vercel.pop("functions", None)
    vercel.update(installCommand="npm ci --include=dev", buildCommand="npm run build",
                  outputDirectory=f"{design_path}/dist", git={"deploymentEnabled": False})
    (target / "vercel.json").write_text(json.dumps(vercel, indent=2) + "\n")
    (target / "delivery-manifest.json").write_text(json.dumps({
        "schema_version": 1, "store_slug": req.slug, "design": req.design,
        "source_repository": req.source_repository, "source_revision": req.target_revision,
        "layout": "isolated",
    }, indent=2) + "\n")
    return output_root
