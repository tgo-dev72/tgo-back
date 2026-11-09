import copy
import json
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from store.models import Store, StoreMember
from . import repository_migration as workflow
from .cloud import remove_github_storefront_app
from .jobs import enqueue_storefront_deployment, execute_storefront_deployment
from .models import StorefrontRequest, StorefrontRepositoryMigration as Migration
from .tests_repositories import REPO_SETTINGS
from .vercel_deploy import DeployError


@override_settings(**{**REPO_SETTINGS, "STOREFRONT_DEPLOYMENT_BRANCH": "main"})
class MigrationWorkflowTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Original business name", slug="canary")
        self.req = StorefrontRequest.objects.create(
            store=self.store, name="Canary", slug="canary", design="basic",
            domain="https://canary.example.com", deployed_url="https://canary.example.com",
            source_repository="owner/source", deployment_repository="owner/source",
            deployment_branch="main", deployment_app_path="apps/canary", deployment_layout="legacy",
            github_commit_sha="b" * 40, vercel_project_id="prj_canary", vercel_deployment_id="dpl_old",
            target_revision="a" * 40, deployed_revision="a" * 40,
            last_known_good_revision="a" * 40, last_known_good_deployment_id="dpl_old",
            pipeline_state="live", status="created", deploy_status="deployed",
        )
        self.provider_repo = "source"
        self.alias = "dpl_old"
        self.calls = []
        self.project = {
            "id": "prj_canary", "rootDirectory": "apps/canary",
            "link": {"type": "github", "org": "owner", "repo": "source", "repoId": 1, "productionBranch": "main"},
        }
        self.provider_patcher = patch.object(workflow, "_provider", side_effect=self.provider)
        self.provider_patcher.start(); self.addCleanup(self.provider_patcher.stop)
        self.get_patcher = patch.object(workflow, "_get_project", side_effect=lambda _: copy.deepcopy(self.project))
        self.get_patcher.start(); self.addCleanup(self.get_patcher.stop)
        self.health_patcher = patch.object(workflow, "storefront_health_is_ready", return_value=True)
        self.health = self.health_patcher.start(); self.addCleanup(self.health_patcher.stop)

    def provider(self, path, method="GET", body=None):
        self.calls.append((path, method, body))
        if path.startswith("/v4/aliases/"):
            return {"projectId": "prj_canary", "deploymentId": self.alias}
        if path.endswith("/link"):
            if method == "DELETE":
                self.project["link"] = None
            elif method == "POST":
                repo = body["repo"].split("/")[-1]
                self.project["link"] = {"type": "github", "org": "owner", "repo": repo, "repoId": 1 if repo == "source" else 2}
            return {}
        if "/promote/" in path or "/rollback/" in path:
            self.alias = path.split("/")[-1]
            if "/rollback/" in path:
                self.project["autoAssignCustomDomains"] = False
            return {}
        if path == "/v9/projects/prj_canary" and method == "PATCH":
            self.project.update(body)
            return copy.deepcopy(self.project)
        if path.startswith("/v13/deployments/"):
            m = Migration.objects.filter(request=self.req).first()
            return {"readyState": "READY", "id": path.split("/")[-1], "projectId":"prj_canary", "target":"production",
                    "readySubstate":"PROMOTED" if path.endswith("dpl_old") else "STAGED",
                    "gitSource":{"sha":"c"*40,"ref":"main"},
                    "meta":{"tijaragoMigration":str(m.pk),"githubCommitOrg":"owner","githubRepo":"delivery"}}
        if path.startswith("/v6/deployments?"):
            return {"deployments": []}
        if path == "/v13/deployments" and method == "POST":
            return {"id": "dpl_candidate", "url": "candidate.vercel.app"}
        raise AssertionError((path, method))

    def prepare(self):
        return workflow.prepare(self.req.pk, confirm_slug="canary", retention_hours=0, approval="Disposable QA migration")

    def candidate(self):
        m = self.prepare()
        m.source.update(tree_sha="old-tree", live_deployment_id="dpl_old", rootDirectory="apps/canary", link=self.project["link"])
        m.destination.update(github_commit_sha="c" * 40, tree_sha="new-tree")
        m.candidate_deployment_id = "dpl_candidate"
        m.candidate_url = "https://candidate.vercel.app"
        m.state = "candidate_ready"
        m.save()
        return m

    def promote(self):
        return workflow.promote(self.req.pk, confirm_slug="canary", evidence="qa/report.json")

    def test_prepare_guards_active_or_unapproved_store_before_provider_access(self):
        self.req.pipeline_state = "building"; self.req.save()
        with self.assertRaisesMessage(DeployError, "Only a live"):
            self.prepare()
        self.assertFalse(Migration.objects.exists()); self.assertEqual(self.calls, [])
        self.req.pipeline_state = "live"; self.req.deployed_revision = "d" * 40; self.req.save()
        with self.assertRaisesMessage(DeployError, "pinned approved"):
            self.prepare()
        self.assertEqual(self.calls, [])

    def test_preparation_is_durable_and_double_prepare_cannot_replace_snapshot(self):
        m = self.prepare()
        with self.assertRaisesMessage(DeployError, "already has"):
            self.prepare()
        self.assertEqual(Migration.objects.get(pk=m.pk).source["deployment_repository"], "owner/source")
        self.assertEqual(self.calls, [])

    def test_current_worker_cannot_be_replaced_by_another_command(self):
        m = self.candidate(); m.action_token = uuid.uuid4(); m.save()
        with self.assertRaisesMessage(DeployError, "Another migration command"):
            self.promote()
        self.assertEqual(self.calls, [])

    def test_failure_after_disconnect_retains_recovery_state_and_original_coordinates(self):
        self.candidate()
        original = workflow._provider.side_effect
        def fail_connect(path, method="GET", body=None):
            if path.endswith("/link") and method == "POST":
                raise DeployError("Provider unavailable")
            return original(path, method, body)
        workflow._provider.side_effect = fail_connect
        with self.assertRaisesMessage(DeployError, "Provider unavailable"):
            self.promote()
        m = Migration.objects.get(request=self.req)
        self.req.refresh_from_db()
        self.assertEqual(m.state, "promoting"); self.assertIsNone(m.action_token)
        self.assertEqual(self.req.deployment_repository, "owner/source")
        self.assertEqual(self.alias, "dpl_old")
        workflow._provider.side_effect = original
        workflow.restore(self.req.pk, confirm_slug="canary")
        self.assertEqual(self.project["link"]["repo"], "source")
        self.assertEqual(Migration.objects.get(request=self.req).state, "rolled_back")

    def test_promotion_failure_after_provider_switch_can_restore_without_reverting_business_data(self):
        self.candidate()
        self.health.side_effect = [True, False, True]
        with self.assertRaisesMessage(DeployError, "not healthy"):
            self.promote()
        self.assertEqual(self.alias, "dpl_candidate")
        self.req.refresh_from_db(); self.assertEqual(self.req.deployment_repository, "owner/source")
        self.store.name = "Order-time merchant edit"; self.store.save()
        workflow.restore(self.req.pk, confirm_slug="canary")
        self.assertEqual(self.alias, "dpl_old")
        self.store.refresh_from_db(); self.assertEqual(self.store.name, "Order-time merchant edit")
        self.assertIsNone(Migration.objects.get(request=self.req).rollback_verified_at)

    @patch.object(workflow, "_tree", return_value="new-tree")
    @patch.object(workflow.cloud, "remove_github_storefront_app", return_value=True)
    def test_migrate_restore_repromote_and_retention_cleanup_preserve_store_identity(self, remove, tree):
        self.candidate()
        self.promote()
        self.req.refresh_from_db()
        self.assertEqual(self.req.deployment_repository, "owner/delivery")
        self.assertEqual(self.req.vercel_project_id, "prj_canary")
        with self.assertRaisesMessage(DeployError, "Rehearse restoration"):
            workflow.finalize(self.req.pk, confirm_slug="canary", approval="QA cleanup")
        remove.assert_not_called()
        workflow.restore(self.req.pk, confirm_slug="canary")
        self.req.refresh_from_db(); self.assertEqual(self.req.deployment_repository, "owner/source")
        self.promote()
        m = Migration.objects.get(request=self.req); m.retain_until = timezone.now() + timedelta(hours=1); m.save()
        with self.assertRaisesMessage(DeployError, "retention period"):
            workflow.finalize(self.req.pk, confirm_slug="canary", approval="QA cleanup")
        remove.assert_not_called()
        m.retain_until = timezone.now() - timedelta(seconds=1); m.save()
        workflow.finalize(self.req.pk, confirm_slug="canary", approval="QA cleanup approved")
        remove.assert_called_once_with("canary", repository="owner/source", branch="main", app_path="apps/canary", expected_tree_sha="old-tree")
        self.req.refresh_from_db(); self.store.refresh_from_db()
        self.assertEqual(self.req.deployed_url, "https://canary.example.com")
        self.assertEqual(self.req.store_id, self.store.pk)
        self.assertEqual(self.store.name, "Original business name")
        self.assertEqual(Migration.objects.get(request=self.req).state, "finalized")

    @patch.object(workflow.cloud, "remove_github_storefront_app")
    @patch.object(workflow, "_tree", return_value="unexpected-tree")
    def test_changed_destination_prevents_old_folder_cleanup(self, tree, remove):
        self.candidate(); self.promote(); workflow.restore(self.req.pk, confirm_slug="canary"); self.promote()
        with self.assertRaisesMessage(DeployError, "Destination bundle changed"):
            workflow.finalize(self.req.pk, confirm_slug="canary", approval="cleanup")
        remove.assert_not_called()

    def test_unrelated_provider_relink_is_not_overwritten(self):
        self.candidate()
        self.project["link"]["repo"] = "unrelated"
        with self.assertRaisesMessage(DeployError, "changed outside"):
            self.promote()
        self.assertFalse(any(method != "GET" for _,method,_ in self.calls))

    def test_lost_candidate_response_is_adopted_without_a_second_deployment(self):
        m = self.candidate(); m.candidate_deployment_id = ""; m.candidate_url = ""; m.state = "creating_candidate"
        m.journal = [{"state":"creating_candidate"}]; m.save()
        original = workflow._provider.side_effect
        def already_created(path, method="GET", body=None):
            if path.startswith("/v6/deployments?"):
                return {"deployments":[{"uid":"dpl_existing", "url":"existing.vercel.app", "target":"production", "meta":{"tijaragoMigration":str(m.pk)}}]}
            return original(path,method,body)
        workflow._provider.side_effect = already_created
        result = workflow.build(self.req.pk, confirm_slug="canary")
        self.assertEqual(result.candidate_deployment_id, "dpl_existing")
        self.assertFalse(any(path == "/v13/deployments" and method == "POST" for path,method,_ in self.calls))

    def test_unknown_candidate_submission_is_not_duplicated(self):
        m = self.candidate(); m.candidate_deployment_id = ""; m.state = "creating_candidate"
        m.journal = [{"state":"creating_candidate"}]; m.save()
        with self.assertRaisesMessage(DeployError, "outcome is unknown"):
            workflow.build(self.req.pk, confirm_slug="canary")
        self.assertFalse(any(path == "/v13/deployments" and method == "POST" for path,method,_ in self.calls))

    def test_same_health_marker_cannot_promote_a_different_generated_commit(self):
        self.candidate()
        original = workflow._provider.side_effect
        def wrong_commit(path, method="GET", body=None):
            data = original(path, method, body)
            if path.startswith("/v13/deployments/"):
                data["gitSource"]["sha"] = "f" * 40
            return data
        workflow._provider.side_effect = wrong_commit
        with self.assertRaisesMessage(DeployError, "Candidate provider identity"):
            self.promote()
        self.assertEqual(self.alias, "dpl_old")
        self.assertEqual(self.project["link"]["repo"], "source")

    def test_restore_uses_provider_rollback_and_preserves_original_assignment_policy(self):
        self.candidate(); self.promote()
        workflow.restore(self.req.pk, confirm_slug="canary")
        self.assertIn(("/v1/projects/prj_canary/rollback/dpl_old", "POST", None), self.calls)
        self.assertTrue(self.project["autoAssignCustomDomains"])

    def test_unrelated_public_deployment_is_not_overwritten(self):
        self.candidate(); self.alias = "dpl_external_change"
        with self.assertRaisesMessage(DeployError, "changed outside this migration"):
            self.promote()
        self.assertEqual(self.alias, "dpl_external_change")
        self.assertFalse(any("/promote/" in path or "/rollback/" in path for path,_,_ in self.calls))

    @patch.object(workflow, "_tree", return_value="old-tree")
    def test_preflight_refuses_a_different_serving_commit_before_publication(self, tree):
        self.prepare()
        with self.assertRaisesMessage(DeployError, "does not match the serving"):
            workflow.build(self.req.pk, confirm_slug="canary")
        self.assertFalse(any(method != "GET" for _,method,_ in self.calls))

    def test_build_stages_production_artifact_without_assigning_public_domain(self):
        m = self.candidate(); m.candidate_deployment_id = ""; m.state = "published"; m.save()
        result = workflow.build(self.req.pk, confirm_slug="canary")
        request = next(body for path,method,body in self.calls if path == "/v13/deployments" and method == "POST")
        self.assertEqual(request["target"], "production")
        self.assertIs(request["autoAssignCustomDomains"], False)
        self.assertEqual(request["gitSource"]["sha"], "c"*40)
        self.req.refresh_from_db()
        self.assertEqual(self.req.deployment_repository, "owner/source")
        self.assertEqual(self.alias, "dpl_old")
        self.assertEqual(result.state, "candidate_ready")

    def test_background_job_is_suppressed_during_migration(self):
        self.prepare()
        with patch("storefront_factory.jobs._launch_local_job") as launch:
            self.assertFalse(enqueue_storefront_deployment(self.req.pk, force=True))
            result = execute_storefront_deployment(self.req.pk, task_id=uuid.uuid4())
        launch.assert_not_called()
        self.assertEqual(result, {"status":"migration_active", "terminal":True})

    def test_owner_delete_is_rejected_before_remote_cleanup(self):
        self.prepare()
        user = get_user_model().objects.create_user(email="migration-owner@test.local", password="only-for-tests-12")
        StoreMember.objects.create(user=user, store=self.store, role=StoreMember.Roles.OWNER)
        client = APIClient(); client.force_authenticate(user)
        with patch("storefront_factory.services.cleanup_storefront_infrastructure") as cleanup:
            response = client.delete(f"/api/store/{self.store.pk}/", {"store_name":"canary","confirmation":"delete my store"},format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "storefront_migration_active")
        cleanup.assert_not_called()
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())

    @override_settings(STOREFRONT_NEW_REPO_ENABLED=False)
    def test_backend_rollback_check_requires_a_quiescent_journal_and_keeps_schema(self):
        self.prepare()
        with self.assertRaisesMessage(CommandError, "Finish or restore/cancel"):
            call_command("check_storefront_rollback", stdout=StringIO())
        workflow.cancel(self.req.pk, confirm_slug="canary")
        output = StringIO(); call_command("check_storefront_rollback", stdout=output)
        self.assertTrue(json.loads(output.getvalue())["expanded_schema_present"])
        self.assertTrue(Migration.objects.filter(request=self.req).exists())

    @patch("storefront_factory.cloud._update_main_ref")
    @patch("storefront_factory.cloud._git_api")
    def test_changed_legacy_subtree_prevents_any_cleanup_commit(self, api, update):
        api.side_effect = [{"object":{"sha":"head"}}, {"tree":[{"path":"apps/canary","type":"tree","sha":"changed"}]}]
        with self.assertRaisesMessage(DeployError, "retained storefront folder changed"):
            remove_github_storefront_app("canary",repository="owner/source",expected_tree_sha="original")
        update.assert_not_called()
        self.assertEqual(api.call_count, 2)
