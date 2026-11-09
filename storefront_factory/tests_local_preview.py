import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .local_preview import _guard, run_local_pipeline
from .services import InfrastructureCleanupError, cleanup_storefront_infrastructure


@override_settings(
    DEBUG=True,
    STOREFRONT_DEPLOYMENT_MODE="local",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    DEV_API_URL="http://127.0.0.1:8001",
)
class LocalPreviewTests(SimpleTestCase):
    def test_local_cleanup_refuses_hosted_store(self):
        hosted = SimpleNamespace(
            pk=8, deployed_url="https://shop.example.com",
            vercel_project_id="prj_real", vercel_deployment_id="dpl_real",
        )
        with self.assertRaisesRegex(InfrastructureCleanupError, "disabled in local mode"):
            cleanup_storefront_infrastructure([hosted])

    @override_settings(STOREFRONT_RELEASE_REVISION="a" * 40)
    @patch("api.designs._fetch_manifest_from_github")
    def test_local_catalog_reads_checkout_even_when_cloud_revision_is_pinned(self, fetch):
        from api.designs import list_designs

        self.assertIn("basic", [design["key"] for design in list_designs()])
        fetch.assert_not_called()

    @patch("storefront_factory.services.remove_github_storefront_app")
    @patch("storefront_factory.services.remove_vercel_project")
    @patch("storefront_factory.services.remove_cloudflare_dns_record")
    @patch("storefront_factory.local_preview.stop_local_preview")
    def test_local_cleanup_never_calls_remote_providers(self, stop, dns, vercel, github):
        preview = SimpleNamespace(
            pk=7, deployed_url="http://127.0.0.1:5190",
            vercel_project_id="", vercel_deployment_id="", deploy_status="deployed",
        )
        self.assertEqual(
            cleanup_storefront_infrastructure([preview]),
            {"dns_records": 0, "vercel_projects": 0, "github_apps": 0},
        )
        stop.assert_called_once_with(7)
        dns.assert_not_called()
        vercel.assert_not_called()
        github.assert_not_called()

    def test_local_mode_requires_sqlite_and_loopback_api(self):
        _guard()
        with override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.postgresql"}}):
            with self.assertRaisesRegex(RuntimeError, "SQLite"):
                _guard()
        with override_settings(DEV_API_URL="https://api.example.com"):
            with self.assertRaisesRegex(RuntimeError, "local backend"):
                _guard()
        with override_settings(DEBUG=False):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                _guard()

    def test_local_pipeline_serves_design_and_records_loopback_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "designs" / "basic"
            design.mkdir(parents=True)
            (design / "package.json").write_text('{"name":"basic"}')
            vite = root / "node_modules" / "vite" / "bin" / "vite.js"
            vite.parent.mkdir(parents=True)
            vite.write_text("")
            marker = root / "preview.json"
            req = SimpleNamespace(
                pk=7, design="basic", slug="acme", PIPELINE_BUILDING="building",
                PIPELINE_LIVE="live", save=Mock(), vercel_project_id="",
                vercel_deployment_id="", github_commit_sha="", deployed_url="",
            )
            process = Mock(pid=12345)
            process.poll.return_value = None
            response = Mock(status=200)
            on_state = Mock()
            with override_settings(DESIGN_STOCK_PATH=str(root)), \
                    patch("storefront_factory.local_preview.shutil.which", return_value="/bin/node"), \
                    patch("storefront_factory.local_preview.subprocess.Popen", return_value=process) as popen, \
                    patch("storefront_factory.local_preview.socket.socket") as sock, \
                    patch("storefront_factory.local_preview.urlopen") as urlopen, \
                    patch("storefront_factory.local_preview.stop_local_preview"), \
                    patch("storefront_factory.local_preview._pid_file", return_value=marker):
                urlopen.return_value.__enter__.return_value = response
                sock.return_value.__enter__.return_value.bind.return_value = None
                run_local_pipeline(req, on_state=on_state)

            self.assertEqual(req.deployed_url, f"http://127.0.0.1:{req.port}")
            self.assertEqual(req.port, 5190)
            self.assertEqual(req.domain, req.deployed_url)
            self.assertEqual(req.app_path, str(design))
            self.assertEqual(json.loads(marker.read_text())["pid"], 12345)
            self.assertEqual(popen.call_args.kwargs["env"]["VITE_STORE_SLUG"], "acme")
            self.assertEqual(popen.call_args.kwargs["env"]["VITE_API_URL"], "http://127.0.0.1:8001")
            self.assertEqual(on_state.call_args_list[-1].args[0], "live")
