import base64
import json
import shutil
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import ReadTimeout
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from store.models import Store

from .deployment_state import ensure_initial_deployment_event, record_deployment_state
from .models import (
    StorefrontCategory,
    StorefrontDeploymentEvent,
    StorefrontRequest,
    validate_category_key,
)
from .scaffold import ScaffoldError, scaffold_storefront
from .cloud import _download_design_stock, _push_app_to_github, _repo_cache, remove_github_storefront_app, validate_pipeline_configuration
from .services import (
    InfrastructureCleanupError,
    cleanup_storefront_infrastructure,
    refresh_deployment_status,
    run_cloud_pipeline,
    storefront_health_is_ready,
)
from .vercel_deploy import (
    DeployError,
    _attach_domain,
    _ensure_project_env_vars,
    _ensure_dns_record,
    _get_project,
    _trigger_deployment,
    _validate_reused_project,
    remove_cloudflare_dns_record,
    remove_vercel_project,
)
from .jobs import (
    _launch_local_job,
    enqueue_storefront_deployment,
    execute_and_reconcile_local_deployment,
    execute_storefront_deployment,
    fail_stalled_storefront_deployment,
    reconcile_storefront_deployment,
)


class ProviderDeletionTests(SimpleTestCase):
    @override_settings(VERCEL_API_TOKEN="vercel-token", VERCEL_TEAM_ID="team_123")
    @patch("storefront_factory.vercel_deploy.requests.delete")
    def test_vercel_project_delete_uses_project_id_and_team(self, request_delete):
        request_delete.return_value = Mock(status_code=204, text="")

        removed = remove_vercel_project("prj_acme")

        self.assertTrue(removed)
        request_delete.assert_called_once()
        args, kwargs = request_delete.call_args
        self.assertEqual(args[0], "https://api.vercel.com/v9/projects/prj_acme")
        self.assertEqual(kwargs["params"], {"teamId": "team_123"})

    @override_settings(
        CLOUDFLARE_API_TOKEN="cloudflare-token",
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
    )
    @patch("storefront_factory.vercel_deploy._cloudflare_zone_id", return_value="zone_123")
    @patch("storefront_factory.vercel_deploy.requests.delete")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_cloudflare_deletes_exact_cname_record(self, request_get, request_delete, _zone):
        lookup = Mock(status_code=200, text="ok")
        lookup.json.return_value = {
            "success": True,
            "result": [
                {"id": "dns_123", "type": "CNAME", "name": "acme.alfirdaousstore.com"},
            ],
        }
        deleted = Mock(status_code=200, text="ok")
        deleted.json.return_value = {"success": True, "result": {"id": "dns_123"}}
        request_get.return_value = lookup
        request_delete.return_value = deleted

        count = remove_cloudflare_dns_record("acme.alfirdaousstore.com")

        self.assertEqual(count, 1)
        request_get.assert_called_once()
        request_delete.assert_called_once_with(
            "https://api.cloudflare.com/client/v4/zones/zone_123/dns_records/dns_123",
            headers={
                "Authorization": "Bearer cloudflare-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )


class GithubSourceDeletionTests(SimpleTestCase):
    @override_settings(GITHUB_TOKEN="github-token", DESIGN_STOCK_REPO="owner/design-stock")
    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_republish_removes_stale_files_without_touching_other_apps(self, git_api, update_ref):
        def api_response(path, method="GET", **_kwargs):
            responses = {
                "git/blobs": {"sha": "new-package"},
                "git/refs/heads/main": {"object": {"sha": "head-1"}},
                "git/trees/head-1?recursive=1": {
                    "sha": "tree-1",
                    "tree": [
                        {"path": "apps/acme/package.json", "type": "blob", "mode": "100644", "sha": "old"},
                        {"path": "apps/acme/stale.ts", "type": "blob", "mode": "100644", "sha": "stale"},
                        {"path": "apps/other/package.json", "type": "blob", "mode": "100644", "sha": "other"},
                    ],
                },
                "git/trees": {"sha": "tree-2"},
                "git/commits": {"sha": "commit-2"},
            }
            return responses[path]

        git_api.side_effect = api_response
        update_ref.return_value = Mock(status_code=200, text="")
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory) / "apps" / "acme"
            app_dir.mkdir(parents=True)
            (app_dir / "package.json").write_text("{}", encoding="utf-8")
            request = SimpleNamespace(slug="acme", github_commit_sha="", save=Mock())

            _push_app_to_github(request, Path(directory), Mock())

        tree_write = next(
            call for call in git_api.call_args_list
            if call.args[0] == "git/trees" and call.kwargs.get("method") == "POST"
        )
        entries = tree_write.kwargs["json"]["tree"]
        self.assertIn(
            {"path": "apps/acme/stale.ts", "mode": "100644", "type": "blob", "sha": None},
            entries,
        )
        self.assertIn(
            {"path": "apps/acme/package.json", "mode": "100644", "type": "blob", "sha": "new-package"},
            entries,
        )
        self.assertFalse(any(entry["path"].startswith("apps/other/") for entry in entries))

    @override_settings(GITHUB_TOKEN="github-token", DESIGN_STOCK_REPO="owner/design-stock")
    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_removes_only_the_exact_storefront_directory(self, git_api, update_ref):
        git_api.side_effect = [
            {"object": {"sha": "head-1"}},
            {
                "sha": "tree-1",
                "tree": [
                    {"path": "apps/acme/package.json", "type": "blob", "mode": "100644", "sha": "a"},
                    {"path": "apps/acme/src/main.tsx", "type": "blob", "mode": "100644", "sha": "b"},
                    {"path": "apps/acme-plus/package.json", "type": "blob", "mode": "100644", "sha": "c"},
                    {"path": "packages/core/index.ts", "type": "blob", "mode": "100644", "sha": "d"},
                ],
            },
            {"sha": "tree-2"},
            {"sha": "commit-2"},
        ]
        update_ref.return_value = Mock(status_code=200, text="")

        removed = remove_github_storefront_app("acme")

        self.assertTrue(removed)
        tree_write = git_api.call_args_list[2]
        self.assertEqual(tree_write.args[0], "git/trees")
        self.assertEqual(
            tree_write.kwargs["json"]["tree"],
            [
                {"path": "apps/acme/package.json", "mode": "100644", "type": "blob", "sha": None},
                {"path": "apps/acme/src/main.tsx", "mode": "100644", "type": "blob", "sha": None},
            ],
        )
        update_ref.assert_called_once_with("commit-2", repository="owner/design-stock", branch="main")

    @override_settings(GITHUB_TOKEN="github-token", DESIGN_STOCK_REPO="owner/design-stock")
    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_missing_storefront_directory_is_an_idempotent_success(self, git_api, update_ref):
        git_api.side_effect = [
            {"object": {"sha": "head-1"}},
            {
                "sha": "tree-1",
                "tree": [{"path": "apps/another/package.json", "type": "blob", "sha": "a"}],
            },
        ]

        self.assertFalse(remove_github_storefront_app("acme"))
        update_ref.assert_not_called()
        self.assertEqual(git_api.call_count, 2)

    @override_settings(GITHUB_TOKEN="github-token", DESIGN_STOCK_REPO="owner/design-stock")
    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_ref_conflict_retries_from_the_newest_main_head(self, git_api, update_ref):
        git_api.side_effect = [
            {"object": {"sha": "head-1"}},
            {"sha": "tree-1", "tree": [{"path": "apps/acme/a", "type": "blob", "sha": "a"}]},
            {"sha": "tree-delete-1"},
            {"sha": "commit-1"},
            {"object": {"sha": "head-2"}},
            {
                "sha": "tree-2",
                "tree": [
                    {"path": "apps/acme/a", "type": "blob", "sha": "a"},
                    {"path": "apps/new-store/a", "type": "blob", "sha": "new"},
                ],
            },
            {"sha": "tree-delete-2"},
            {"sha": "commit-2"},
        ]
        update_ref.side_effect = [
            Mock(status_code=409, text="conflict"),
            Mock(status_code=200, text=""),
        ]

        self.assertTrue(remove_github_storefront_app("acme"))
        self.assertEqual(update_ref.call_count, 2)
        second_commit = git_api.call_args_list[7]
        self.assertEqual(second_commit.kwargs["json"]["parents"], ["head-2"])

    @patch("storefront_factory.cloud._git_api")
    def test_invalid_slug_fails_before_any_github_request(self, git_api):
        with self.assertRaises(DeployError):
            remove_github_storefront_app("../packages")
        git_api.assert_not_called()

    @override_settings(GITHUB_TOKEN="github-token", DESIGN_STOCK_REPO="owner/design-stock")
    @patch("storefront_factory.cloud._git_api")
    def test_truncated_repository_tree_fails_closed(self, git_api):
        git_api.side_effect = [
            {"object": {"sha": "head-1"}},
            {"sha": "tree-1", "tree": [], "truncated": True},
        ]

        with self.assertRaises(DeployError):
            remove_github_storefront_app("acme")


class ProviderCheckpointTests(SimpleTestCase):
    @override_settings(
        VERCEL_API_TOKEN="vercel-token",
        VERCEL_TEAM_ID="",
        VERCEL_API_TIMEOUT=7,
        VERCEL_API_READ_ATTEMPTS=3,
    )
    @patch("storefront_factory.vercel_deploy.time.sleep")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_project_lookup_retries_a_transient_tls_timeout(self, request_get, sleep):
        recovered = Mock(status_code=200)
        recovered.json.return_value = {"id": "prj_recovered"}
        request_get.side_effect = [ReadTimeout("TLS handshake timed out"), recovered]

        project = _get_project("retry-store")

        self.assertEqual(project["id"], "prj_recovered")
        self.assertEqual(request_get.call_count, 2)
        request_get.assert_called_with(
            "https://api.vercel.com/v9/projects/retry-store",
            headers={"Authorization": "Bearer vercel-token", "Content-Type": "application/json"},
            params={},
            timeout=7,
        )
        sleep.assert_called_once_with(1)

    @override_settings(VERCEL_API_TOKEN="vercel-token", VERCEL_TEAM_ID="")
    @patch("storefront_factory.vercel_deploy.requests.post")
    def test_vercel_id_is_checkpointed_before_readiness_poll(self, request_post):
        response = Mock(status_code=201, text="ok")
        response.json.return_value = {
            "id": "dpl_checkpoint",
            "url": "checkpoint.vercel.app",
            "readyState": "READY",
        }
        request_post.return_value = response
        checkpoint = Mock()

        result = _trigger_deployment(
            SimpleNamespace(slug="checkpoint"),
            {"id": "prj_checkpoint", "link": {"repoId": 123}},
            "main",
            Mock(),
            on_created=checkpoint,
        )

        checkpoint.assert_called_once_with(
            "dpl_checkpoint",
            "https://checkpoint.vercel.app",
        )
        self.assertEqual(result.deployment_id, "dpl_checkpoint")

    @override_settings(
        CLOUDFLARE_API_TOKEN="cloudflare-token",
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
    )
    @patch("storefront_factory.vercel_deploy._cloudflare_zone_id", return_value="zone_123")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_existing_dns_record_id_is_returned_for_checkpointing(self, request_get, _zone):
        response = Mock(status_code=200)
        response.json.return_value = {
            "success": True,
            "result": [{
                "id": "dns_checkpoint",
                "type": "CNAME",
                "content": "cname.vercel-dns.com",
                "proxied": False,
            }],
        }
        request_get.return_value = response

        record_id = _ensure_dns_record("checkpoint.alfirdaousstore.com", Mock())

        self.assertEqual(record_id, "dns_checkpoint")

    @override_settings(
        CLOUDFLARE_API_TOKEN="cloudflare-token",
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
    )
    @patch("storefront_factory.vercel_deploy._cloudflare_zone_id", return_value="zone_123")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_existing_dns_record_with_wrong_target_fails_closed(self, request_get, _zone):
        response = Mock(status_code=200)
        response.json.return_value = {
            "success": True,
            "result": [{
                "id": "dns_wrong",
                "type": "CNAME",
                "content": "unrelated.example.com",
                "proxied": False,
            }],
        }
        request_get.return_value = response

        with self.assertRaises(DeployError):
            _ensure_dns_record("checkpoint.alfirdaousstore.com", Mock())

    @override_settings(VERCEL_API_TOKEN="token", VERCEL_TEAM_ID="")
    @patch("storefront_factory.vercel_deploy.requests.post")
    @patch("storefront_factory.vercel_deploy.requests.patch")
    @patch("storefront_factory.vercel_deploy.requests.get")
    def test_existing_vercel_env_value_is_updated_not_silently_kept(
        self, request_get, request_patch, request_post,
    ):
        listing = Mock(status_code=200)
        listing.json.return_value = {"envs": [{"id": "env_api", "key": "VITE_API_URL"}]}
        request_get.return_value = listing
        request_patch.return_value = Mock(status_code=200)

        _ensure_project_env_vars("prj_test", {"VITE_API_URL": "https://api.example"}, Mock())

        request_patch.assert_called_once()
        self.assertEqual(request_patch.call_args.kwargs["json"]["value"], "https://api.example")
        request_post.assert_not_called()

    @override_settings(DESIGN_STOCK_REPO="owner/design-stock")
    def test_reused_vercel_project_with_wrong_root_is_rejected(self):
        request = SimpleNamespace(slug="acme")
        with self.assertRaises(DeployError):
            _validate_reused_project(
                request,
                {"rootDirectory": "apps/another-store", "link": {"repo": "design-stock"}},
            )

    @override_settings(VERCEL_API_TOKEN="token", VERCEL_TEAM_ID="")
    @patch("storefront_factory.vercel_deploy.requests.get")
    @patch("storefront_factory.vercel_deploy.requests.post")
    def test_domain_conflict_is_only_idempotent_for_the_same_project(
        self, request_post, request_get,
    ):
        request_post.return_value = Mock(status_code=409, text="already assigned")
        request_get.return_value = Mock(status_code=404)

        with self.assertRaises(DeployError):
            _attach_domain("acme.example.com", "prj_acme", Mock())

        request_get.return_value = Mock(status_code=200)
        _attach_domain("acme.example.com", "prj_acme", Mock())


class SparseDesignSourceTests(SimpleTestCase):
    def tearDown(self):
        _repo_cache.update(path=None, ts=0.0, sha=None, design=None)

    @patch("storefront_factory.cloud._git_api")
    def test_fetches_only_the_selected_design_tree(self, git_api):
        revision = "a" * 40

        def response(path, method="GET", **_kwargs):
            if path == f"git/commits/{revision}":
                return {"tree": {"sha": "tree-root"}}
            if path == "git/trees/tree-root?recursive=1":
                return {
                    "truncated": False,
                    "tree": [
                        {"path": "designs/basic/package.json", "type": "blob", "sha": "basic-package"},
                        {"path": "designs/story/package.json", "type": "blob", "sha": "story-package"},
                        {"path": "apps/existing/package.json", "type": "blob", "sha": "existing-app"},
                    ],
                }
            if path == "git/blobs/basic-package":
                return {
                    "encoding": "base64",
                    "content": base64.b64encode(b'{"name":"@firdaous/basic"}').decode("ascii"),
                }
            raise AssertionError(f"Unexpected GitHub request: {path}")

        git_api.side_effect = response
        logs = []

        root = _download_design_stock(logs.append, revision=revision, design="basic")

        self.addCleanup(shutil.rmtree, root, True)
        self.assertEqual((root / "designs/basic/package.json").read_text(), '{"name":"@firdaous/basic"}')
        self.assertFalse((root / "designs/story").exists())
        self.assertFalse((root / "apps/existing").exists())
        self.assertEqual((root / ".tijarago-source-revision").read_text(), revision)
        self.assertNotIn("git/blobs/story-package", [call.args[0] for call in git_api.call_args_list])


class PipelinePreflightTests(SimpleTestCase):
    @override_settings(
        GITHUB_TOKEN="",
        VERCEL_API_TOKEN="vercel",
        CLOUDFLARE_API_TOKEN="cloudflare",
        DESIGN_STOCK_REPO="owner/design-stock",
        STOREFRONT_API_URL="https://api.example.com",
        STOREFRONT_BASE_DOMAIN="example.com",
    )
    def test_missing_provider_configuration_fails_before_pipeline_side_effects(self):
        with self.assertRaises(DeployError):
            validate_pipeline_configuration()

    @override_settings(
        GITHUB_TOKEN="github",
        VERCEL_API_TOKEN="vercel",
        CLOUDFLARE_API_TOKEN="cloudflare",
        DESIGN_STOCK_REPO="owner/design-stock",
        STOREFRONT_API_URL="http://private-api.local",
        STOREFRONT_BASE_DOMAIN="example.com",
    )
    def test_non_https_runtime_api_is_rejected(self):
        with self.assertRaises(DeployError):
            validate_pipeline_configuration()

    @override_settings(
        GITHUB_TOKEN="github",
        VERCEL_API_TOKEN="vercel",
        CLOUDFLARE_API_TOKEN="cloudflare",
        DESIGN_STOCK_REPO="owner/design-stock",
        STOREFRONT_API_URL="https://api.example.com",
        STOREFRONT_BASE_DOMAIN="example.com",
        STOREFRONT_RELEASE_REVISION="main",
    )
    def test_mutable_release_reference_is_rejected(self):
        with self.assertRaises(DeployError):
            validate_pipeline_configuration()


class StorefrontHealthTests(SimpleTestCase):
    @patch("storefront_factory.services.requests.get")
    def test_health_requires_matching_store_identity(self, request_get):
        response = Mock(status_code=200)
        response.json.return_value = {"store_slug": "acme", "design": "basic"}
        request_get.return_value = response

        self.assertTrue(storefront_health_is_ready("https://acme.example", "acme"))
        self.assertFalse(storefront_health_is_ready("https://acme.example", "other"))
        request_get.assert_called_with(
            "https://acme.example/_tijarago/health.json",
            timeout=15,
            allow_redirects=True,
        )

    @patch("storefront_factory.services.requests.get")
    def test_health_requires_the_expected_release_when_provided(self, request_get):
        response = Mock(status_code=200)
        response.json.return_value = {
            "store_slug": "acme",
            "design": "basic",
            "release_revision": "a" * 40,
        }
        request_get.return_value = response

        self.assertTrue(
            storefront_health_is_ready(
                "https://acme.example",
                "acme",
                "a" * 40,
            )
        )
        self.assertFalse(
            storefront_health_is_ready(
                "https://acme.example",
                "acme",
                "b" * 40,
            )
        )

    @patch("storefront_factory.services.requests.get")
    def test_health_rejects_html_or_generic_success_page(self, request_get):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("not json")
        request_get.return_value = response

        self.assertFalse(storefront_health_is_ready("https://acme.example", "acme"))


class InfrastructureCleanupTests(SimpleTestCase):
    @override_settings(
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
        GITHUB_TOKEN="github-token",
        VERCEL_API_TOKEN="vercel-token",
        CLOUDFLARE_API_TOKEN="cloudflare-token",
    )
    @patch("storefront_factory.services.remove_github_storefront_app", return_value=True)
    @patch("storefront_factory.services.remove_vercel_project", return_value=True)
    @patch("storefront_factory.services.remove_cloudflare_dns_record", return_value=1)
    def test_removes_managed_dns_vercel_and_github_source(self, remove_dns, remove_project, remove_github):
        deployment = SimpleNamespace(
            slug="acme",
            domain="https://acme.alfirdaousstore.com",
            vercel_project_id="prj_acme",
            deployed_url="https://acme.alfirdaousstore.com",
            deploy_status="deployed",
        )

        result = cleanup_storefront_infrastructure([deployment])

        self.assertEqual(result, {"dns_records": 1, "vercel_projects": 1, "github_apps": 1})
        remove_dns.assert_called_once_with("acme.alfirdaousstore.com")
        remove_project.assert_called_once_with("prj_acme")
        remove_github.assert_called_once_with("acme", repository=settings.DESIGN_STOCK_REPO, branch="main", app_path="apps/acme")

    @override_settings(
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
        GITHUB_TOKEN="github-token",
        VERCEL_API_TOKEN="vercel-token",
        CLOUDFLARE_API_TOKEN="",
    )
    @patch("storefront_factory.services.remove_github_storefront_app", return_value=True)
    @patch("storefront_factory.services.remove_vercel_project", return_value=False)
    @patch("storefront_factory.services.remove_cloudflare_dns_record")
    def test_custom_domain_dns_is_not_deleted(self, remove_dns, remove_project, remove_github):
        deployment = SimpleNamespace(
            slug="acme",
            domain="https://shop.merchant.example",
            vercel_project_id="prj_acme",
            deployed_url="https://shop.merchant.example",
            deploy_status="deployed",
        )

        cleanup_storefront_infrastructure([deployment])

        remove_dns.assert_not_called()
        remove_project.assert_called_once_with("prj_acme")
        remove_github.assert_called_once_with("acme", repository=settings.DESIGN_STOCK_REPO, branch="main", app_path="apps/acme")

    @override_settings(
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
        GITHUB_TOKEN="github-token",
        VERCEL_API_TOKEN="",
        CLOUDFLARE_API_TOKEN="cloudflare-token",
    )
    @patch("storefront_factory.services.remove_github_storefront_app")
    @patch("storefront_factory.services.remove_vercel_project")
    @patch("storefront_factory.services.remove_cloudflare_dns_record")
    def test_missing_credentials_fail_before_provider_mutation(self, remove_dns, remove_project, remove_github):
        deployment = SimpleNamespace(
            slug="acme",
            domain="https://acme.alfirdaousstore.com",
            vercel_project_id="prj_acme",
            deployed_url="https://acme.alfirdaousstore.com",
            deploy_status="deployed",
        )

        with self.assertRaises(InfrastructureCleanupError):
            cleanup_storefront_infrastructure([deployment])

        remove_dns.assert_not_called()
        remove_project.assert_not_called()
        remove_github.assert_not_called()

    @override_settings(
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
        GITHUB_TOKEN="",
        VERCEL_API_TOKEN="vercel-token",
        CLOUDFLARE_API_TOKEN="cloudflare-token",
    )
    @patch("storefront_factory.services.remove_github_storefront_app")
    @patch("storefront_factory.services.remove_vercel_project")
    @patch("storefront_factory.services.remove_cloudflare_dns_record")
    def test_missing_github_credential_fails_before_provider_mutation(
        self, remove_dns, remove_project, remove_github,
    ):
        deployment = SimpleNamespace(
            slug="acme",
            domain="https://acme.alfirdaousstore.com",
            vercel_project_id="prj_acme",
            deployed_url="https://acme.alfirdaousstore.com",
            deploy_status="deployed",
        )

        with self.assertRaises(InfrastructureCleanupError):
            cleanup_storefront_infrastructure([deployment])

        remove_dns.assert_not_called()
        remove_project.assert_not_called()
        remove_github.assert_not_called()

    @override_settings(
        STOREFRONT_BASE_DOMAIN="alfirdaousstore.com",
        DESIGN_STOCK_REPO="owner/design-stock",
        GITHUB_TOKEN="github-token",
        VERCEL_API_TOKEN="vercel-token",
        CLOUDFLARE_API_TOKEN="cloudflare-token",
    )
    @patch(
        "storefront_factory.services.remove_github_storefront_app",
        side_effect=DeployError("GitHub unavailable"),
    )
    @patch("storefront_factory.services.remove_vercel_project", return_value=True)
    @patch("storefront_factory.services.remove_cloudflare_dns_record", return_value=1)
    def test_github_failure_is_reported_as_retryable_infrastructure_cleanup(
        self, _remove_dns, _remove_project, _remove_github,
    ):
        deployment = SimpleNamespace(
            slug="acme",
            domain="https://acme.alfirdaousstore.com",
            vercel_project_id="prj_acme",
            deployed_url="https://acme.alfirdaousstore.com",
            deploy_status="deployed",
        )

        with self.assertRaises(InfrastructureCleanupError):
            cleanup_storefront_infrastructure([deployment])


class DeploymentReadinessTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(slug="readiness-test", name="Readiness Test")
        self.request = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
            vercel_deployment_id="dpl_test",
            deployed_url="https://readiness-test.example",
        )

    @patch("storefront_factory.services.get_deployment_state", return_value="QUEUED")
    def test_queued_deployment_remains_non_terminal(self, get_state):
        refresh_deployment_status(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)
        get_state.assert_called_once_with("dpl_test")

    @patch("storefront_factory.services.storefront_health_is_ready", return_value=True)
    @patch("storefront_factory.services.get_deployment_state", return_value="READY")
    def test_ready_and_publicly_reachable_deployment_becomes_live(self, _get_state, _health):
        refresh_deployment_status(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_CREATED)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYED)
        _health.assert_called_once_with(
            "https://readiness-test.example",
            "readiness-test",
            None,
        )

    @patch("storefront_factory.services.get_deployment_state", return_value="ERROR")
    def test_failed_deployment_becomes_terminal(self, _get_state):
        refresh_deployment_status(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_FAILED)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_FAILED)
        self.assertIn("status ERROR", self.request.deploy_log)

    @patch("storefront_factory.services.storefront_health_is_ready", return_value=False)
    @patch("storefront_factory.services.get_deployment_state", return_value="READY")
    def test_provider_ready_but_unreachable_domain_remains_verifying(self, _get_state, _health):
        refresh_deployment_status(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_VERIFYING)
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)

    @patch(
        "storefront_factory.services.get_deployment_state",
        side_effect=DeployError("temporary provider error"),
    )
    def test_transient_status_error_does_not_fail_deployment(self, _get_state):
        refresh_deployment_status(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)


class DeploymentTruthTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(slug="truth-store", name="Truth Store")
        self.request = StorefrontRequest.objects.create(
            store=self.store,
            design="basic",
            name=self.store.name,
            slug=self.store.slug,
            status=StorefrontRequest.STATUS_SCAFFOLDING,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYING,
        )

    def test_events_are_monotonic_and_never_expose_internal_errors(self):
        ensure_initial_deployment_event(self.request.pk)
        record_deployment_state(
            self.request.pk,
            StorefrontRequest.PIPELINE_PREPARING,
            message="deployment.preparing",
        )
        record_deployment_state(
            self.request.pk,
            StorefrontRequest.PIPELINE_FAILED,
            message="deployment.failed",
            internal_error="secret provider response",
        )

        events = list(StorefrontDeploymentEvent.objects.filter(storefront=self.request))
        self.assertEqual([event.version for event in events], [1, 2, 3])
        self.assertEqual(events[-1].snapshot["log"], "")
        self.assertNotIn("secret provider response", json.dumps(events[-1].snapshot))
        self.request.refresh_from_db()
        self.assertEqual(self.request.internal_error, "secret provider response")

    def test_verified_live_state_promotes_only_the_requested_release(self):
        self.request.target_revision = "b" * 40
        self.request.vercel_deployment_id = "dpl_verified"
        self.request.save(update_fields=["target_revision", "vercel_deployment_id"])

        record_deployment_state(
            self.request.pk,
            StorefrontRequest.PIPELINE_LIVE,
            message="deployment.live",
        )

        self.request.refresh_from_db()
        self.assertEqual(self.request.deployed_revision, "b" * 40)
        self.assertEqual(self.request.last_known_good_revision, "b" * 40)
        self.assertEqual(self.request.last_known_good_deployment_id, "dpl_verified")

    @patch("storefront_factory.jobs._publish_vercel_job", return_value=True)
    @patch("storefront_factory.jobs._is_vercel_runtime", return_value=True)
    def test_enqueue_records_task_id_and_deduplicates(self, _runtime, publish):
        self.assertTrue(enqueue_storefront_deployment(self.request.pk, oidc_token="oidc-token"))
        self.assertFalse(enqueue_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        self.assertIsNotNone(self.request.task_id)
        publish.assert_called_once_with(self.request.pk, self.request.task_id, "oidc-token")

    @patch("storefront_factory.jobs._launch_local_job", return_value=True)
    @patch("storefront_factory.jobs._is_vercel_runtime", return_value=False)
    def test_local_enqueue_launches_detached_worker_instead_of_running_inline(
        self,
        _runtime,
        launch,
    ):
        self.assertTrue(enqueue_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        launch.assert_called_once_with(self.request.pk, self.request.task_id)
        self.assertEqual(self.request.attempt_count, 0)

    @override_settings(BASE_DIR=Path("/tmp/tijarago-worker-test"))
    @patch("storefront_factory.jobs.subprocess.Popen")
    def test_local_worker_is_detached_from_the_http_process(self, popen):
        task_id = uuid.uuid4()

        self.assertTrue(_launch_local_job(self.request.pk, task_id))

        args, kwargs = popen.call_args
        self.assertEqual(args[0][2], "execute_storefront_deployment")
        self.assertEqual(args[0][3:], [str(self.request.pk), str(task_id)])
        self.assertEqual(kwargs["cwd"], "/tmp/tijarago-worker-test")
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])

    @patch("storefront_factory.jobs.close_old_connections")
    @patch("storefront_factory.jobs.reconcile_storefront_deployment")
    @patch("storefront_factory.jobs.execute_storefront_deployment")
    def test_local_worker_reconciles_until_provider_truth_is_terminal(
        self,
        execute,
        reconcile,
        close_connections,
    ):
        task_id = uuid.uuid4()
        execute.return_value = {"status": "building", "terminal": False}
        reconcile.side_effect = [
            {"status": "building", "terminal": False},
            {"status": "live", "terminal": True},
        ]
        sleeper = Mock()

        result = execute_and_reconcile_local_deployment(
            self.request.pk,
            task_id=task_id,
            sleeper=sleeper,
        )

        self.assertEqual(result, {"status": "live", "terminal": True})
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(close_connections.call_count, 2)
        self.assertEqual(
            [call.kwargs["attempt"] for call in reconcile.call_args_list],
            [1, 2],
        )
        self.assertTrue(all(call.kwargs["task_id"] == task_id for call in reconcile.call_args_list))

    @patch("storefront_factory.jobs.reconcile_storefront_deployment")
    @patch("storefront_factory.jobs.execute_storefront_deployment")
    def test_local_worker_does_not_reconcile_terminal_execution(self, execute, reconcile):
        execute.return_value = {"status": "failed", "terminal": True}

        result = execute_and_reconcile_local_deployment(
            self.request.pk,
            task_id=uuid.uuid4(),
            sleeper=Mock(),
        )

        self.assertEqual(result, {"status": "failed", "terminal": True})
        reconcile.assert_not_called()

    @patch("storefront_factory.jobs._launch_local_job", side_effect=OSError("worker unavailable"))
    @patch("storefront_factory.jobs._is_vercel_runtime", return_value=False)
    def test_local_worker_start_failure_is_retryable(self, _runtime, _launch):
        self.assertFalse(enqueue_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_FAILED)

    @patch("storefront_factory.jobs.refresh_deployment_status")
    @patch("storefront_factory.jobs.run_cloud_pipeline")
    @patch("storefront_factory.jobs.sync_categories_from_store")
    def test_executor_uses_database_state_and_provider_reconciliation(
        self,
        sync_categories,
        run_pipeline,
        refresh_status,
    ):
        task_id = uuid.uuid4()
        self.request.task_id = task_id
        self.request.save(update_fields=["task_id"])

        result = execute_storefront_deployment(self.request.pk, task_id=task_id)

        self.request.refresh_from_db()
        self.assertEqual(self.request.attempt_count, 1)
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_PREPARING)
        sync_categories.assert_called_once()
        run_pipeline.assert_called_once()
        refresh_status.assert_called_once()
        self.assertEqual(result, {"status": "preparing", "terminal": False})

    @patch("storefront_factory.jobs.execute_storefront_deployment")
    @patch("storefront_factory.jobs._publish_vercel_job", side_effect=RuntimeError("queue down"))
    @patch("storefront_factory.jobs._is_vercel_runtime", return_value=True)
    def test_queue_failure_is_retryable_and_never_runs_inline_on_vercel(
        self,
        _runtime,
        _publish,
        execute,
    ):
        self.assertFalse(enqueue_storefront_deployment(self.request.pk, oidc_token="oidc-token"))
        self.request.refresh_from_db()
        execute.assert_not_called()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)

    def test_accepted_job_without_an_executor_attempt_becomes_retryable(self):
        task_id = uuid.uuid4()
        self.request.task_id = task_id
        self.request.last_heartbeat_at = timezone.now() - timedelta(minutes=7)
        self.request.save(update_fields=["task_id", "last_heartbeat_at"])

        changed = fail_stalled_storefront_deployment(self.request.pk)

        self.assertTrue(changed)
        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_FAILED)
        self.assertEqual(self.request.public_message, "deployment.failed")

    def test_accepted_job_without_task_id_also_becomes_retryable(self):
        self.request.last_heartbeat_at = timezone.now() - timedelta(minutes=7)
        self.request.save(update_fields=["last_heartbeat_at"])

        self.assertTrue(fail_stalled_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)

    def test_abandoned_preparation_without_provider_handoff_becomes_retryable(self):
        self.request.task_id = uuid.uuid4()
        self.request.pipeline_state = StorefrontRequest.PIPELINE_PREPARING
        self.request.attempt_count = 1
        self.request.last_heartbeat_at = timezone.now() - timedelta(minutes=7)
        self.request.save(update_fields=["task_id", "pipeline_state", "attempt_count", "last_heartbeat_at"])

        self.assertTrue(fail_stalled_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_FAILED)

    def test_recent_or_claimed_jobs_are_not_marked_stalled(self):
        self.request.task_id = uuid.uuid4()
        self.request.last_heartbeat_at = timezone.now() - timedelta(minutes=7)
        self.request.attempt_count = 1
        self.request.save(update_fields=["task_id", "last_heartbeat_at", "attempt_count"])

        self.assertFalse(fail_stalled_storefront_deployment(self.request.pk))

        self.request.refresh_from_db()
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_ACCEPTED)

    def test_reconciliation_timeout_is_terminal_and_safe(self):
        task_id = uuid.uuid4()
        self.request.task_id = task_id
        self.request.pipeline_state = StorefrontRequest.PIPELINE_BUILDING
        self.request.vercel_deployment_id = "dpl_timeout"
        self.request.save(update_fields=["task_id", "pipeline_state", "vercel_deployment_id"])

        with patch("storefront_factory.jobs.refresh_deployment_status"):
            result = reconcile_storefront_deployment(
                self.request.pk,
                task_id=task_id,
                attempt=90,
            )

        self.request.refresh_from_db()
        self.assertEqual(result, {"status": "failed", "terminal": True})
        self.assertEqual(self.request.pipeline_state, StorefrontRequest.PIPELINE_FAILED)
        self.assertEqual(self.request.public_message, "deployment.timeout")

    @patch("storefront_factory.internal_views.execute_storefront_deployment")
    def test_private_executor_requires_exact_capability(self, execute):
        task_id = uuid.uuid4()
        self.request.task_id = task_id
        self.request.save(update_fields=["task_id"])
        execute.return_value = {"status": "building", "terminal": False}

        denied = self.client.post(
            f"/api/internal/storefront-deployments/{self.request.pk}/execute",
            HTTP_X_STOREFRONT_TASK=str(uuid.uuid4()),
        )
        allowed = self.client.post(
            f"/api/internal/storefront-deployments/{self.request.pk}/execute",
            HTTP_X_STOREFRONT_TASK=str(task_id),
        )

        self.assertEqual(denied.status_code, 404)
        self.assertEqual(allowed.status_code, 200)
        execute.assert_called_once_with(self.request.pk, task_id=task_id)

    @patch("storefront_factory.services.run_pipeline")
    def test_pipeline_does_not_claim_queued_deployment_is_live(self, run_pipeline):
        def finish_pending(req, on_log=None, on_state=None):
            req.vercel_project_id = "prj_test"
            req.vercel_deployment_id = "dpl_test"
            req.deployed_url = "https://readiness-test.example.com"
            req._vercel_ready_state = "BUILDING"
            if on_log:
                on_log("Deployment still BUILDING")
            return "Deployment still BUILDING"

        run_pipeline.side_effect = finish_pending

        run_cloud_pipeline(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)
        self.assertEqual(self.request.vercel_deployment_id, "dpl_test")

    @patch("storefront_factory.services.run_pipeline")
    def test_pipeline_marks_only_ready_deployment_live(self, run_pipeline):
        def finish_ready(req, on_log=None, on_state=None):
            req.vercel_project_id = "prj_test"
            req.vercel_deployment_id = "dpl_test"
            req.deployed_url = "https://readiness-test.example.com"
            req._vercel_ready_state = "READY"
            return "Deployment is READY"

        run_pipeline.side_effect = finish_ready

        run_cloud_pipeline(self.request)

        self.request.refresh_from_db()
        self.assertEqual(self.request.status, StorefrontRequest.STATUS_SCAFFOLDING)
        self.assertEqual(self.request.deploy_status, StorefrontRequest.DEPLOY_DEPLOYING)


def make_template(repo_root: Path):
    template = repo_root / "designs" / "classic"
    template.mkdir(parents=True)
    (template / "package.json").write_text('{"name": "@firdaous/classic", "scripts": {}}')
    (template / "vite.config.ts").write_text(
        "export default { server: { port: Number(env.PORT ?? 5173) } };\n"
    )
    (template / "index.html").write_text("<html><head><title>Classic</title></head></html>")
    (template / "src").mkdir()
    (template / "src/assets").mkdir()
    (template / "src/config").mkdir()
    (template / "src/config/store.config.ts").write_text("placeholder\n")
    (template / "src/config/taxonomy.config.ts").write_text("placeholder\n")
    (template / "src/locales").mkdir()
    for lang in ("en", "fr", "ar"):
        (template / f"src/locales/{lang}.ts").write_text('export const ui = { productTypes: { soap: "Soap" } };\n')
    return template


def make_runtime_template(repo_root: Path):
    """Mirror current bootStorefront templates: no generated config/locales/assets."""
    template = repo_root / "designs" / "classic"
    template.mkdir(parents=True)
    (template / "package.json").write_text(
        json.dumps({"name": "@platform/classic", "scripts": {"build": "vite build"}})
    )
    (template / "vite.config.ts").write_text(
        "export default { server: { port: Number(env.PORT ?? 5182) } };\n"
    )
    (template / "index.html").write_text("<html><head><title>Classic</title></head></html>")
    (template / "src").mkdir()
    (template / "src/main.tsx").write_text("// Runtime-configured storefront entry.\n")
    return template


class CategoryKeyValidatorTests(TestCase):
    def test_valid_keys_pass(self):
        for key in ("soap", "soap-1", "personal-care-2"):
            validate_category_key(key)

    def test_invalid_keys_rejected(self):
        for key in ("Soap", "soap_1", "soap 1", "soap!", "-soap", "soap-", "söap", "soap.1"):
            with self.assertRaises(ValidationError):
                validate_category_key(key)


class ScaffoldCleanupTests(TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        (self.repo / "manifest.json").write_text(json.dumps({"designs": [{"key": "classic"}]}))
        self.store = Store.objects.create(slug="scaffold-test", name="Scaffold Test")
        self.req = StorefrontRequest.objects.create(
            name="Test Store",
            slug="test-app",
            design="classic",
            store=self.store,
        )
        StorefrontCategory.objects.create(storefront=self.req, key="soap", label="Soap")
        StorefrontCategory.objects.create(
            storefront=self.req, key="body-care", label="Body Care",
        )

    def _scaffold(self, **kwargs):
        settings_override = {"DESIGN_STOCK_PATH": str(self.repo)}
        settings_override.update(kwargs)
        return override_settings(**settings_override)

    @patch("storefront_factory.scaffold.subprocess.run")
    def test_failure_removes_partial_dir(self, mock_run):
        # minimal template missing package.json → the rewrite step after
        # copytree must fail, clean apps/<slug> up, and raise ScaffoldError.
        (self.repo / "designs" / "classic").mkdir(parents=True)
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        with self._scaffold():
            with self.assertRaises(ScaffoldError):
                scaffold_storefront(self.req)
        self.assertFalse((self.repo / "apps" / "test-app").exists())

    @patch("storefront_factory.scaffold.subprocess.run")
    def test_success_creates_app(self, mock_run):
        make_template(self.repo)
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""

        with self._scaffold(STOREFRONT_API_URL="https://api.example.com"):
            log = scaffold_storefront(self.req)
        target = self.repo / "apps" / "test-app"
        self.assertTrue(target.is_dir())
        self.assertIn("Done", log)
        self.assertIn(
            f'name": "@firdaous/test-app"',
            (target / "package.json").read_text(),
        )
        # taxonomy regenerated from the request's categories
        self.assertIn('key: "soap"', (target / "src/config/taxonomy.config.ts").read_text())
        taxonomy = (target / "src/config/taxonomy.config.ts").read_text()
        self.assertIn('"body-care": "Body Care"', taxonomy)
        for lang in ("en", "fr", "ar"):
            self.assertIn(
                '"body-care": \'Body Care\'',
                (target / f"src/locales/{lang}.ts").read_text(),
            )
        marker = json.loads((target / "public/_tijarago/health.json").read_text())
        self.assertEqual(
            marker,
            {
                "store_slug": "test-app",
                "design": "classic",
                "release_revision": "",
            },
        )
        vercel_config = json.loads((target / "vercel.json").read_text())
        rewrites = vercel_config["rewrites"]
        self.assertEqual(rewrites[0]["source"], "/_tijarago/health.json")
        self.assertEqual(rewrites[1], {
            "source": "/robots.txt",
            "destination": "https://api.example.com/api/public/storefront/test-app/robots.txt",
        })
        self.assertEqual(rewrites[2], {
            "source": "/sitemap.xml",
            "destination": "https://api.example.com/api/public/storefront/test-app/sitemap.xml",
        })
        self.assertEqual(rewrites[-1], {"source": "/(.*)", "destination": "/api/seo-render?path=/$1"})
        self.assertNotIn("functions", vercel_config)
        self.assertEqual(vercel_config["buildCommand"], "npm run build && node scripts/prepare-seo-shell.mjs")
        renderer = (target / "api/seo-render.mjs").read_text()
        self.assertIn('import { STOREFRONT_HTML } from "./_seo-shell.mjs";', renderer)
        self.assertEqual((target / "api/_seo-shell.mjs").read_text(), 'export const STOREFRONT_HTML = "";\n')
        self.assertTrue((target / "scripts/prepare-seo-shell.mjs").exists())
        self.assertIn('replaceAll("<", "\\\\u003c")', renderer)
        headers = {item["key"]: item["value"] for item in vercel_config["headers"][0]["headers"]}
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-eval'", headers["Content-Security-Policy"])
        self.assertIn("https://fonts.googleapis.com", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("includeSubDomains", headers["Strict-Transport-Security"])

    def test_runtime_config_template_without_legacy_directories_scaffolds(self):
        make_runtime_template(self.repo)

        with self._scaffold():
            log = scaffold_storefront(self.req, skip_subprocess=True)

        target = self.repo / "apps" / "test-app"
        self.assertTrue(target.is_dir())
        self.assertFalse((target / "src/config").exists())
        self.assertFalse((target / "src/locales").exists())
        self.assertFalse((target / "src/assets").exists())
        self.assertEqual(
            json.loads((target / "package.json").read_text())["name"],
            "@firdaous/test-app",
        )
        self.assertIn("Using runtime store configuration", log)
        self.assertIn("Kept template-owned visual assets", log)
        self.assertEqual(
            json.loads((target / "public/_tijarago/health.json").read_text())["store_slug"],
            "test-app",
        )
