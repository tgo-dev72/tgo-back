"""Store-scoped email forwarding contract; provider calls stay mocked."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .email_routing import alias_for
from .models import Store, StoreConfig, StoreMember


@override_settings(
    STORE_EMAIL_DOMAIN="mail.tijarago.com",
    CLOUDFLARE_EMAIL_ROUTING_TOKEN="test-token",
    CLOUDFLARE_ACCOUNT_ID="test-account",
    CLOUDFLARE_ZONE_ID="test-zone",
)
class StoreEmailForwardingTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(email="owner@example.test", password="TestPassword123!")
        self.other = user_model.objects.create_user(email="other@example.test", password="TestPassword123!")
        self.store = Store.objects.create(name="Test store", slug="test-store")
        StoreMember.objects.create(store=self.store, user=self.owner, role=StoreMember.Roles.OWNER)
        self.client = APIClient()
        self.url = f"/api/store/{self.store.pk}/email-forwarding/"

    def test_alias_uses_email_subdomain_and_only_owner_can_manage(self):
        self.assertEqual(alias_for(self.store), "test-store@mail.tijarago.com")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {"destination": "other@example.test"}).status_code, 404)

    @patch("store.email_routing.destination_for", return_value={"email": "owner@example.test", "verified": None})
    @patch("store.email_routing.ensure_destination", return_value={"email": "owner@example.test", "verified": None})
    def test_unverified_destination_does_not_activate(self, ensure, lookup):
        self.client.force_authenticate(self.owner)
        with patch("store.email_routing.activate") as activate:
            response = self.client.post(self.url, {"destination": "owner@example.test"})
        self.assertEqual(response.status_code, 200)
        config = StoreConfig.objects.get(store=self.store)
        self.assertEqual(config.email_forward_pending, "owner@example.test")
        self.assertEqual(config.email_forward_active, "")
        activate.assert_not_called()

    @patch("store.email_routing.destination_for", return_value={"email": "owner@example.test", "verified": "2026-10-11T00:00:00Z"})
    @patch("store.email_routing.activate", return_value="rule-id")
    def test_activate_only_after_verified(self, activate, destination):
        config = StoreConfig.objects.get(store=self.store)
        config.email_forward_pending = "owner@example.test"
        config.save(update_fields=["email_forward_pending"])
        self.client.force_authenticate(self.owner)
        response = self.client.post(self.url, {"action": "activate"})
        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertEqual(config.email_forward_active, "owner@example.test")
        self.assertEqual(config.email_forward_rule_id, "rule-id")
        self.assertEqual(config.email_forward_pending, "")
        activate.assert_called_once_with(self.store, "owner@example.test")

    @patch("store.email_routing.destination_for", return_value={"email": "owner@example.test", "verified": None})
    def test_pending_verification_cannot_activate(self, destination):
        config = StoreConfig.objects.get(store=self.store)
        config.email_forward_pending = "owner@example.test"
        config.save(update_fields=["email_forward_pending"])
        self.client.force_authenticate(self.owner)
        response = self.client.post(self.url, {"action": "activate"})
        self.assertEqual(response.status_code, 409)
        config.refresh_from_db()
        self.assertEqual(config.email_forward_active, "")

    @patch("store.email_routing.destination_for", return_value={"email": "new@example.test", "verified": None})
    @patch("store.email_routing.ensure_destination", return_value={"email": "new@example.test", "verified": None})
    def test_changing_destination_preserves_active_route_until_verified(self, ensure, destination):
        config = StoreConfig.objects.get(store=self.store)
        config.email_forward_active = "old@example.test"
        config.email_forward_rule_id = "old-rule"
        config.save(update_fields=["email_forward_active", "email_forward_rule_id"])
        self.client.force_authenticate(self.owner)
        response = self.client.post(self.url, {"destination": "new@example.test"})
        self.assertEqual(response.status_code, 200)
        config.refresh_from_db()
        self.assertEqual(config.email_forward_active, "old@example.test")
        self.assertEqual(config.email_forward_rule_id, "old-rule")
        self.assertEqual(config.email_forward_pending, "new@example.test")

    @patch("store.email_routing._list")
    @patch("store.email_routing._request")
    def test_foreign_rule_cannot_be_overwritten(self, request, listing):
        from .email_routing import EmailRoutingError, activate

        listing.return_value = iter([{
            "id": "foreign-rule", "name": "Managed elsewhere",
            "matchers": [{"type": "literal", "field": "to", "value": "test-store@mail.tijarago.com"}],
        }])
        with self.assertRaises(EmailRoutingError):
            activate(self.store, "owner@example.test")
        request.assert_not_called()

    @patch("store.email_routing._list")
    @patch("store.email_routing._request")
    def test_owned_rule_is_updated_with_verified_destination(self, request, listing):
        from .email_routing import activate

        listing.return_value = iter([{
            "id": "old-rule", "name": f"TijaraGO store {self.store.pk}",
            "matchers": [{"type": "literal", "field": "to", "value": "test-store@mail.tijarago.com"}],
        }])
        request.return_value = {"result": {"id": "old-rule"}}
        self.assertEqual(activate(self.store, "owner@example.test"), "old-rule")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertTrue(args[1].endswith("/old-rule"))
        self.assertEqual(kwargs["payload"]["actions"], [{"type": "forward", "value": ["owner@example.test"]}])
