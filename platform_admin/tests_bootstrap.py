from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from store.models import Store
from subscriptions.models import SubscriptionPlan
from theme_previewer.models import ThemeDemoStore


READY_DESIGN = {
    "key": "basic",
    "name": "Basic",
    "status": "ready",
    "tier": "standard",
    "tagline": "A clear storefront",
    "description": "A reliable commerce experience.",
}


class BootstrapPlatformCommandTests(TestCase):
    @patch(
        "platform_admin.management.commands.bootstrap_platform.list_designs",
        return_value=[],
    )
    def test_fails_when_the_release_manifest_is_unavailable(self, _list_designs):
        with self.assertRaisesMessage(CommandError, "No ready themes were found"):
            call_command("bootstrap_platform", stdout=StringIO())

    @patch(
        "platform_admin.management.commands.bootstrap_platform.list_designs",
        return_value=[READY_DESIGN],
    )
    @patch("theme_previewer.provisioning.get_design", return_value=READY_DESIGN)
    def test_prepares_required_data_idempotently(self, _get_design, _list_designs):
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            call_command("bootstrap_platform", stdout=StringIO())
            counts = (
                Store.objects.count(),
                ThemeDemoStore.objects.count(),
                SubscriptionPlan.objects.count(),
            )
            call_command("bootstrap_platform", stdout=StringIO())

        profile = ThemeDemoStore.objects.get(theme_key="basic")
        self.assertEqual(counts, (
            Store.objects.count(),
            ThemeDemoStore.objects.count(),
            SubscriptionPlan.objects.count(),
        ))
        self.assertEqual(profile.store.slug, "demo-basic")
        self.assertFalse(profile.is_enabled)
        self.assertEqual(profile.deployment_status, ThemeDemoStore.DeploymentStatus.PREPARED)

    @patch(
        "platform_admin.management.commands.bootstrap_platform.list_designs",
        return_value=[READY_DESIGN],
    )
    @patch("theme_previewer.provisioning.get_design", return_value=READY_DESIGN)
    @patch(
        "platform_admin.management.commands.bootstrap_platform.Command._deploy_and_enable"
    )
    def test_deploy_mode_enables_external_provisioning(
        self, deploy_and_enable, _get_design, _list_designs,
    ):
        deploy_and_enable.side_effect = lambda profile: profile
        with TemporaryDirectory() as media_root, self.settings(MEDIA_ROOT=media_root):
            call_command("bootstrap_platform", deploy=True, stdout=StringIO())
        deploy_and_enable.assert_called_once()
