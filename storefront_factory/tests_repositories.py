import base64
import importlib
import json
import shutil
import tempfile
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.apps import apps
from django.db import connection
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from .bundle import prepare_isolated_bundle, project_lockfile
from .cloud import _git_api, _download_design_stock, _push_app_to_github, _repo_cache, _update_main_ref, run_pipeline
from .models import StorefrontCategory, StorefrontRequest
from .repositories import destination, github_token, pin_repository_coordinates
from .scaffold import ScaffoldError
from .services import InfrastructureCleanupError, cleanup_storefront_infrastructure
from .vercel_deploy import DeployError, _create_project, _validate_reused_project


REPO_SETTINGS = dict(
    DESIGN_STOCK_REPO="owner/source", GITHUB_TOKEN="legacy-writer",
    STOREFRONT_SOURCE_TOKEN="source-reader",
    STOREFRONT_DEPLOYMENT_REPO="owner/delivery",
    STOREFRONT_DEPLOYMENT_BRANCH="production", STOREFRONT_DEPLOYMENT_TOKEN="delivery-writer",
    STOREFRONT_NEW_REPO_ENABLED=True,
    STOREFRONT_RELEASE_REVISION="a" * 40, VERCEL_API_TOKEN="vercel",
    CLOUDFLARE_API_TOKEN="cloudflare", STOREFRONT_BASE_DOMAIN="example.com",
    STOREFRONT_API_URL="https://api.example.com",
)


@override_settings(**REPO_SETTINGS)
class RepositoryCoordinatesTests(TestCase):
    def setUp(self):
        self.req = StorefrontRequest.objects.create(name="Canary", slug="canary", design="basic")

    def test_new_store_pins_destination_and_does_not_follow_setting_changes(self):
        pin_repository_coordinates(self.req)
        self.req.refresh_from_db()
        self.assertEqual(destination(self.req), ("owner/delivery", "production", "apps/canary"))
        self.assertEqual(self.req.source_repository, "owner/source")
        self.assertEqual(self.req.deployment_layout, "isolated")
        with override_settings(STOREFRONT_DEPLOYMENT_REPO="owner/another", STOREFRONT_DEPLOYMENT_BRANCH="main"):
            pin_repository_coordinates(self.req)
        self.assertEqual(destination(self.req), ("owner/delivery", "production", "apps/canary"))

    def test_existing_remote_request_stays_legacy(self):
        self.req.github_commit_sha = "b" * 40
        pin_repository_coordinates(self.req)
        self.assertEqual(destination(self.req), ("owner/source", "main", "apps/canary"))
        self.assertEqual(self.req.deployment_layout, "legacy")

    def test_data_migration_pins_even_failed_and_unstarted_requests_to_legacy(self):
        migration = importlib.import_module("storefront_factory.migrations.0010_repository_coordinates")
        migration.pin_existing_requests(apps, SimpleNamespace(connection=connection))
        self.req.refresh_from_db()
        pin_repository_coordinates(self.req)
        self.assertEqual(destination(self.req), ("owner/source", "main", "apps/canary"))

    @patch("storefront_factory.cloud._git_api")
    def test_missing_target_token_never_falls_back_to_source_writer(self, api):
        with override_settings(STOREFRONT_DEPLOYMENT_TOKEN=""):
            with self.assertRaises(DeployError):
                run_pipeline(self.req)
        api.assert_not_called()
        self.req.refresh_from_db()
        self.assertEqual(self.req.deployment_repository, "owner/delivery")

    @patch("storefront_factory.cloud._git_api")
    def test_unpinned_source_release_is_rejected_before_network(self, api):
        with override_settings(STOREFRONT_RELEASE_REVISION=""):
            with self.assertRaises(DeployError):
                run_pipeline(self.req)
        api.assert_not_called()

    @patch("storefront_factory.cloud._get_project")
    @patch("storefront_factory.cloud._git_api")
    def test_root_workspace_rejected_before_any_write(self, api, project):
        api.side_effect = [{"private": True}, {"object": {"sha": "head"}}, {"tree": [{"path": "package.json"}]}]
        with self.assertRaisesMessage(DeployError, "root npm workspace"):
            run_pipeline(self.req)
        self.assertTrue(all(call.kwargs.get("method", "GET") == "GET" for call in api.call_args_list))
        project.assert_not_called()

    @patch("storefront_factory.cloud._git_api", return_value={"private": False})
    def test_public_target_rejected_before_upload(self, api):
        with self.assertRaisesMessage(DeployError, "must be private"):
            run_pipeline(self.req)
        self.assertEqual(api.call_count, 1)

    def test_empty_target_retains_legacy_mode_for_new_requests(self):
        with override_settings(STOREFRONT_DEPLOYMENT_REPO=""):
            pin_repository_coordinates(self.req)
        self.assertEqual(destination(self.req), ("owner/source", "main", "apps/canary"))
        self.assertEqual(self.req.deployment_layout, "legacy")

    def test_rollback_switch_keeps_existing_new_repo_credentials_available(self):
        pin_repository_coordinates(self.req)
        fresh = StorefrontRequest.objects.create(name="Fresh", slug="fresh", design="basic")
        with override_settings(STOREFRONT_NEW_REPO_ENABLED=False):
            pin_repository_coordinates(self.req)
            pin_repository_coordinates(fresh)
            self.assertEqual(self.req.deployment_repository, "owner/delivery")
            self.assertEqual(github_token(self.req.deployment_repository, write=True), "delivery-writer")
            self.assertEqual(destination(fresh), ("owner/source", "main", "apps/fresh"))

    @patch("storefront_factory.cloud.deploy_storefront_cloud")
    @patch("storefront_factory.cloud._push_app_to_github")
    @patch("storefront_factory.cloud._get_project", return_value=None)
    @patch("storefront_factory.cloud._git_api")
    @patch("storefront_factory.cloud._download_design_stock")
    def test_pipeline_packages_pinned_source_without_mutating_cached_source(self, download, api, project, publish, deploy):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            IsolatedBundleTests().make_source(source)
            (source / "designs/basic/vite.config.ts").write_text("export default {};\n")
            (source / "designs/basic/index.html").write_text("<title>Example</title>")
            (source / ".tijarago-source-revision").write_text("a" * 40)
            download.return_value = source
            api.side_effect = [{"private": True}, {"object": {"sha": "head"}}, {"tree": []}]
            StorefrontCategory.objects.create(storefront=self.req, key="general", label="General")
            def inspect_bundle(req, root, log):
                bundle = root / "apps/canary"
                self.assertTrue((bundle / "packages/storefront-core/package.json").exists())
                marker = json.loads((bundle / "designs/basic/public/_tijarago/health.json").read_text())
                self.assertEqual(marker["release_revision"], "a" * 40)
                self.assertEqual(req.deployment_repository, "owner/delivery")
                self.assertFalse((bundle / "designs/basic/.env").exists())
            publish.side_effect = inspect_bundle
            run_pipeline(self.req)
            self.assertFalse((source / "apps/canary/public").exists())
            self.assertEqual(json.loads((source / "apps/canary/package.json").read_text())["name"], "@test/canary")
            self.assertEqual(download.call_args.kwargs["repository"], "owner/source")
            self.assertTrue(download.call_args.kwargs["include_dependencies"])
            deploy.assert_called_once()


@override_settings(**REPO_SETTINGS)
class RepositoryRoutingTests(SimpleTestCase):
    @patch("storefront_factory.cloud.requests.request")
    def test_preflight_reads_repository_metadata_without_trailing_slash(self, request):
        responses = {
            "https://api.github.com/repos/owner/source/git/commits/" + "a" * 40: {"sha": "a" * 40},
            "https://api.github.com/repos/owner/delivery": {"private": True},
            "https://api.github.com/repos/owner/delivery/git/refs/heads/production": {"object": {"sha": "head"}},
            "https://api.github.com/repos/owner/delivery/git/trees/head": {"tree": []},
        }

        def github_response(method, url, **kwargs):
            self.assertEqual(method, "GET")
            payload = responses.get(url)
            return Mock(status_code=200 if payload is not None else 404,
                        json=Mock(return_value=payload), text="Not Found")

        request.side_effect = github_response
        output = StringIO()
        call_command("check_storefront_repositories", stdout=output)
        self.assertIn("Read-only checks passed", output.getvalue())
        self.assertEqual(request.call_count, 4)

    @patch("storefront_factory.cloud.requests.patch")
    def test_ref_update_never_forces_and_uses_recorded_branch(self, update):
        _update_main_ref("new-sha", repository="owner/delivery", branch="production")
        self.assertEqual(update.call_args.args[0], "https://api.github.com/repos/owner/delivery/git/refs/heads/production")
        self.assertEqual(update.call_args.kwargs["json"], {"sha": "new-sha", "force": False})
        self.assertEqual(update.call_args.kwargs["headers"]["Authorization"], "Bearer delivery-writer")

    @patch("storefront_factory.cloud._git_api")
    @patch("storefront_factory.management.commands.check_storefront_repositories._git_api")
    def test_preflight_command_only_reads_and_does_not_print_credentials(self, source, target):
        source.return_value = {"sha": "a" * 40}
        target.side_effect = [{"private": True}, {"object": {"sha": "head"}}, {"tree": []}]
        output = StringIO()
        call_command("check_storefront_repositories", stdout=output)
        self.assertIn("Read-only checks passed", output.getvalue())
        self.assertNotIn("source-reader", output.getvalue())
        self.assertNotIn("delivery-writer", output.getvalue())
        self.assertTrue(all(c.kwargs.get("method", "GET") == "GET" for c in target.call_args_list))

    @patch("storefront_factory.cloud.run_pipeline", return_value="same pipeline")
    def test_local_deploy_uses_same_pinned_pipeline(self, pipeline):
        from .vercel_deploy import deploy_storefront
        req = SimpleNamespace(slug="canary")
        self.assertEqual(deploy_storefront(req), "same pipeline")
        pipeline.assert_called_once_with(req)

    @patch("storefront_factory.cloud.requests.request")
    def test_source_and_delivery_use_separate_credentials(self, request):
        request.return_value = Mock(status_code=200, json=Mock(return_value={}))
        _git_api("git/trees/head")
        self.assertIn("/repos/owner/source/", request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer source-reader")
        _git_api("git/blobs", method="POST", repository="owner/delivery", write=True, json={})
        self.assertIn("/repos/owner/delivery/", request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer delivery-writer")
        self.assertEqual(github_token("owner/source", write=True), "legacy-writer")
        with self.assertRaises(ValueError):
            github_token("unconfigured/repo", write=True)

    @patch("storefront_factory.vercel_deploy.requests.post")
    def test_vercel_uses_recorded_destination_and_rejects_wrong_owner(self, post):
        req = SimpleNamespace(slug="canary", deployment_repository="owner/delivery",
                              deployment_branch="production", deployment_app_path="apps/canary")
        post.return_value = Mock(status_code=201, json=Mock(return_value={}))
        _create_project(req, Mock())
        self.assertEqual(post.call_args.kwargs["json"]["gitRepository"]["repo"], "owner/delivery")
        self.assertEqual(post.call_args.kwargs["json"]["rootDirectory"], "apps/canary")
        with self.assertRaises(DeployError):
            _validate_reused_project(req, {"rootDirectory": "apps/canary", "link": {"repo": "delivery", "org": "other"}})

    @patch("storefront_factory.services.remove_github_storefront_app", return_value=True)
    @patch("storefront_factory.services.remove_vercel_project", return_value=True)
    @patch("storefront_factory.services.remove_cloudflare_dns_record", return_value=1)
    def test_cleanup_routes_mixed_legacy_and_new_stores(self, dns, vercel, github):
        def req(slug, repo):
            return SimpleNamespace(slug=slug, deployment_repository=repo,
                deployment_branch="main", deployment_app_path=f"apps/{slug}",
                domain=f"https://{slug}.example.com", vercel_project_id=f"prj_{slug}",
                deploy_status="deployed", deployed_url="https://example.com")
        cleanup_storefront_infrastructure([req("old", "owner/source"), req("new", "owner/delivery")])
        self.assertEqual([c.kwargs["repository"] for c in github.call_args_list], ["owner/source", "owner/delivery"])
        for mock in (dns, vercel, github):
            mock.reset_mock()
        broken = req("old", "owner/source")
        broken.deployment_app_path = "packages"
        with self.assertRaises(InfrastructureCleanupError):
            cleanup_storefront_infrastructure([broken])
        for mock in (dns, vercel, github):
            mock.assert_not_called()

    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_publish_rebases_on_conflict_uses_destination_and_excludes_secrets(self, api, update):
        heads = iter(["head1", "head2"])
        def response(path, **kwargs):
            if path == "git/blobs":
                return {"sha": "blob"}
            if path == "git/refs/heads/production":
                return {"object": {"sha": next(heads)}}
            if path.startswith("git/trees/head"):
                return {"sha": "base", "tree": [{"path": "apps/other/file", "type": "blob", "sha": "other"}]}
            return {"sha": "new"}
        api.side_effect = response
        update.side_effect = [Mock(status_code=409), Mock(status_code=200)]
        req = SimpleNamespace(slug="canary", deployment_repository="owner/delivery",
            deployment_branch="production", deployment_app_path="apps/canary", save=Mock())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "apps/canary"
            app.mkdir(parents=True)
            (app / "package.json").write_text("{}")
            (app / ".env").write_text("SECRET=never-publish")
            _push_app_to_github(req, root, Mock())
        self.assertEqual(sum(c.args[0] == "git/blobs" for c in api.call_args_list), 1)
        for call in api.call_args_list:
            self.assertEqual(call.kwargs["repository"], "owner/delivery")
            self.assertTrue(call.kwargs["write"])
        commits = [c for c in api.call_args_list if c.args[0] == "git/commits"]
        self.assertEqual(commits[-1].kwargs["json"]["parents"], ["head2"])
        for call in update.call_args_list:
            self.assertEqual(call.kwargs, {"repository": "owner/delivery", "branch": "production"})

    @patch("storefront_factory.cloud.time.sleep")
    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_publish_retries_transient_blob_failure(self, api, update, sleep):
        blob_attempts = 0

        def response(path, **kwargs):
            nonlocal blob_attempts
            if path == "git/blobs":
                blob_attempts += 1
                if blob_attempts == 1:
                    raise DeployError("GitHub API POST git/blobs failed (400): resubmit")
                return {"sha": "blob"}
            if path == "git/refs/heads/main":
                return {"object": {"sha": "head"}}
            if path == "git/trees/head?recursive=1":
                return {"sha": "base", "tree": []}
            return {"sha": "new"}

        api.side_effect = response
        update.return_value = Mock(status_code=200)
        req = SimpleNamespace(
            slug="canary", deployment_repository="owner/delivery",
            deployment_branch="main", deployment_app_path="apps/canary", save=Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "apps/canary"
            app.mkdir(parents=True)
            (app / "package.json").write_text("{}")
            _push_app_to_github(req, root, Mock())

        self.assertEqual(blob_attempts, 2)
        sleep.assert_called_once_with(0.5)

    @patch("storefront_factory.cloud._git_api")
    def test_dependency_fetch_is_pinned_sparse_and_cache_separated(self, api):
        revision = "c" * 40
        files = ["designs/basic/package.json", "packages/storefront-core/package.json", "package.json", "package-lock.json"]
        def response(path, **kwargs):
            if path.startswith("git/commits/"):
                return {"tree": {"sha": "root"}}
            if path.startswith("git/trees/"):
                return {"tree": [{"path": p, "sha": str(i), "type": "blob"} for i, p in enumerate(
                    files + ["apps/private/.env", "designs/other/package.json", "packages/core/.env"]) ]}
            return {"encoding": "base64", "content": base64.b64encode(b"{}").decode()}
        api.side_effect = response
        _repo_cache.update(path=None)
        self.addCleanup(_repo_cache.update, path=None)
        for dependencies in (False, True):
            root = _download_design_stock(Mock(), revision, "basic", include_dependencies=dependencies)
            import shutil
            self.addCleanup(shutil.rmtree, root, True)
            self.assertEqual((root / "package-lock.json").exists(), dependencies)
            self.assertFalse((root / "apps").exists())
            self.assertFalse((root / "packages/core/.env").exists())
            self.assertFalse((root / "designs/other").exists())

    @patch("storefront_factory.cloud._git_api")
    def test_dependency_files_do_not_mask_missing_selected_design(self, api):
        revision = "d" * 40

        def response(path, **kwargs):
            if path.startswith("git/commits/"):
                return {"tree": {"sha": "root"}}
            if path.startswith("git/trees/"):
                return {
                    "tree": [
                        {"path": "package.json", "sha": "root-package", "type": "blob"},
                        {"path": "packages/storefront-core/package.json", "sha": "core-package", "type": "blob"},
                    ]
                }
            raise AssertionError(f"Unexpected blob fetch for missing design: {path}")

        api.side_effect = response
        _repo_cache.update(path=None)
        self.addCleanup(_repo_cache.update, path=None)

        with self.assertRaisesMessage(
            ScaffoldError,
            f"Design 'atelier' is unavailable in storefront release {revision[:12]}.",
        ):
            _download_design_stock(
                Mock(), revision, "atelier", include_dependencies=True
            )

    @patch("storefront_factory.cloud._git_api")
    def test_invalid_cached_source_is_downloaded_again(self, api):
        revision = "e" * 40
        with tempfile.TemporaryDirectory() as stale_directory:
            _repo_cache.update(
                path=stale_directory,
                ts=time.time(),
                sha=revision,
                design="atelier",
                repository="owner/source",
                dependencies=False,
            )

            def response(path, **kwargs):
                if path.startswith("git/commits/"):
                    return {"tree": {"sha": "root"}}
                if path.startswith("git/trees/"):
                    return {
                        "tree": [
                            {"path": "designs/atelier/package.json", "sha": "package", "type": "blob"}
                        ]
                    }
                if path == "git/blobs/package":
                    return {
                        "encoding": "base64",
                        "content": base64.b64encode(b"{}").decode(),
                    }
                raise AssertionError(path)

            api.side_effect = response
            root = _download_design_stock(
                Mock(), revision, "atelier", repository="owner/source"
            )
            self.addCleanup(shutil.rmtree, root, True)
            self.assertNotEqual(root, Path(stale_directory))
            self.assertTrue((root / "designs/atelier/package.json").is_file())


class IsolatedBundleTests(SimpleTestCase):
    def make_source(self, root):
        def write(path, value):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value if isinstance(value, str) else json.dumps(value))
        manifest = {"name": "source", "version": "1.0.0", "workspaces": ["packages/*", "designs/*", "apps/*"]}
        write("package.json", manifest)
        write("designs/basic/package.json", {"name": "@test/basic", "dependencies": {"@storefront/core": "*", "react": "18.3.1"}})
        write("packages/storefront-core/package.json", {"name": "@storefront/core"})
        write("apps/canary/package.json", {"name": "@test/canary"})
        write("apps/canary/vercel.json", {"rewrites": [{"source": "/(.*)", "destination": "/"}]})
        write("apps/canary/api/seo-render.mjs", 'import { STOREFRONT_HTML } from "./_seo-shell.mjs";')
        write("apps/canary/api/_seo-shell.mjs", 'export const STOREFRONT_HTML = "";')
        write("apps/canary/scripts/prepare-seo-shell.mjs", 'const APP_PATH = ".";')
        write("apps/canary/.env", {"secret": "not-published"})
        write("package-lock.json", {"lockfileVersion": 3, "packages": {
            "": manifest, "designs/basic": {"name": "@test/basic", "dependencies": {"@storefront/core": "*", "react": "18.3.1"}},
            "packages/storefront-core": {"name": "@storefront/core"},
            "apps/dashboard": {}, "node_modules/@test/dashboard": {"link": True, "resolved": "apps/dashboard"},
            "node_modules/@storefront/core": {"link": True, "resolved": "packages/storefront-core"},
            "node_modules/react": {"version": "18.3.1", "integrity": "original-integrity"},
        }})

    def test_bundle_is_self_contained_and_retains_exact_lock_resolutions(self):
        req = SimpleNamespace(slug="canary", design="basic", source_repository="owner/source", target_revision="a" * 40)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_source(root / "source")
            prepare_isolated_bundle(req, root / "source", root / "output")
            bundle = root / "output/apps/canary"
            self.assertTrue((bundle / "packages/storefront-core/package.json").exists())
            self.assertFalse((bundle / "designs/basic/.env").exists())
            lock = json.loads((bundle / "package-lock.json").read_text())
            self.assertNotIn("apps/dashboard", lock["packages"])
            self.assertNotIn("node_modules/@test/dashboard", lock["packages"])
            self.assertEqual(lock["packages"]["node_modules/react"]["integrity"], "original-integrity")
            config = json.loads((bundle / "vercel.json").read_text())
            self.assertEqual(config["installCommand"], "npm ci --include=dev")
            self.assertEqual(config["outputDirectory"], "designs/basic/dist")
            self.assertEqual(config["git"], {"deploymentEnabled": False})
            self.assertNotIn("functions", config)
            self.assertIn("node scripts/prepare-seo-shell.mjs", json.loads((bundle / "package.json").read_text())["scripts"]["build"])
            self.assertIn('const APP_PATH = "designs/basic";', (bundle / "scripts/prepare-seo-shell.mjs").read_text())
            self.assertTrue((bundle / "api/_seo-shell.mjs").exists())
            self.assertEqual(json.loads((bundle / "delivery-manifest.json").read_text())["source_revision"], "a" * 40)

    def test_missing_lockfile_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            req = SimpleNamespace(slug="canary", design="basic")
            with self.assertRaises(ScaffoldError):
                prepare_isolated_bundle(req, Path(directory), Path(directory) / "output")

    def test_projection_preserves_nested_versions_and_discards_unrelated_tooling(self):
        records = {
            "designs/basic": {"dependencies": {"a": "1", "x": "2"}},
            "node_modules/a": {"version": "1", "dependencies": {"x": "1"}},
            "node_modules/a/node_modules/x": {"version": "1"},
            "node_modules/x": {"version": "2"},
            "node_modules/unrelated": {"version": "99"},
        }
        projected = project_lockfile({"packages": records}, {"name": "test", "version": "1"}, ["designs/basic"])
        self.assertEqual(projected["packages"]["node_modules/a/node_modules/x"]["version"], "1")
        self.assertEqual(projected["packages"]["node_modules/x"]["version"], "2")
        self.assertNotIn("node_modules/unrelated", projected["packages"])
        del records["node_modules/x"]
        with self.assertRaises(ScaffoldError):
            project_lockfile({"packages": records}, {"name": "test", "version": "1"}, ["designs/basic"])
