import base64
import hashlib
import hmac
import json
import tempfile
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from store.models import Store, StoreMember
from marketing.models import Subscriber

from .models import MailConversation, MailMessage, StoreMailbox
from .services import apply_resend_event


def signed(body, secret="inbound-secret"):
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"HTTP_X_TIJARAGO_TIMESTAMP": timestamp, "HTTP_X_TIJARAGO_SIGNATURE": signature}


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    STORE_EMAIL_DOMAIN="mail.tijarago.com",
    MAILBOX_INBOUND_SECRET="inbound-secret",
    RESEND_WEBHOOK_SECRET="whsec_" + base64.b64encode(b"webhook-secret").decode(),
)
class StoreMailboxTests(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        User = get_user_model()
        self.owner = User.objects.create_user(email="owner@example.test", password="SafePass123!")
        self.other = User.objects.create_user(email="other@example.test", password="SafePass123!")
        self.store = Store.objects.create(name="Test Store", slug="teststore")
        self.other_store = Store.objects.create(name="Other Store", slug="otherstore")
        StoreMember.objects.create(store=self.store, user=self.owner, role=StoreMember.Roles.OWNER)
        StoreMember.objects.create(store=self.other_store, user=self.other, role=StoreMember.Roles.OWNER)
        StoreMailbox.objects.create(store=self.store, enabled=True)
        self.client = APIClient()

    def inbound(self, *, message_id="<incoming-1@example.test>", subject="A question"):
        payload = {
            "from": "Ada Customer <ada@example.test>",
            "to": "teststore@mail.tijarago.com",
            "subject": subject,
            "text": "Can you help?",
            "html": "<p>Can you help?</p>",
            "message_id": message_id,
            "in_reply_to": "",
            "references": [],
            "attachments": [],
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        return self.client.post(
            reverse("cloudflare-mail-inbound"), data=body, content_type="application/json", **signed(body),
        )

    def test_signed_inbound_creates_thread_and_deduplicates(self):
        response = self.inbound()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(MailConversation.objects.get().unread_count, 1)
        self.assertEqual(MailMessage.objects.get().sender_email, "ada@example.test")

        replay = self.inbound()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(MailMessage.objects.count(), 1)

    def test_inbound_rejects_invalid_signature_and_unknown_mailbox(self):
        payload = {"from": "a@example.test", "to": "missing@mail.tijarago.com"}
        body = json.dumps(payload).encode()
        rejected = self.client.post(reverse("cloudflare-mail-inbound"), body, content_type="application/json")
        self.assertEqual(rejected.status_code, 401)
        unknown = self.client.post(
            reverse("cloudflare-mail-inbound"), body, content_type="application/json", **signed(body),
        )
        self.assertEqual(unknown.status_code, 404)

    def test_disabled_mailbox_rejects_inbound_and_outbound(self):
        StoreMailbox.objects.filter(store=self.store).update(enabled=False)
        self.assertEqual(self.inbound().status_code, 404)
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-compose", args=[self.store.pk]),
            {"to": "customer@example.test", "subject": "Hello", "text": "Body"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)

    def test_mailbox_is_tenant_isolated_and_marks_thread_read(self):
        self.inbound()
        conversation = MailConversation.objects.get()
        self.client.force_authenticate(self.other)
        denied = self.client.get(reverse("store-mail-detail", args=[self.store.pk, conversation.public_id]))
        self.assertEqual(denied.status_code, 404)

        self.client.force_authenticate(self.owner)
        response = self.client.get(reverse("store-mail-detail", args=[self.store.pk, conversation.public_id]))
        self.assertEqual(response.status_code, 200)
        conversation.refresh_from_db()
        self.assertEqual(conversation.unread_count, 0)

    def test_reply_sends_as_store_and_preserves_thread_headers(self):
        self.inbound()
        conversation = MailConversation.objects.get()
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-reply", args=[self.store.pk, conversation.public_id]),
            {"text": "Yes, we can help.", "html": "<p>Yes, we can help.</p>"}, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["ada@example.test"])
        self.assertEqual(sent.from_email, "Test Store <teststore@mail.tijarago.com>")
        self.assertEqual(sent.extra_headers["In-Reply-To"], "<incoming-1@example.test>")
        self.assertEqual(MailMessage.objects.filter(direction="outbound", status="sent").count(), 1)

    def test_archive_search_and_unread_actions(self):
        self.inbound(subject="Wholesale request")
        conversation = MailConversation.objects.get()
        self.client.force_authenticate(self.owner)
        listed = self.client.get(reverse("store-mail-list", args=[self.store.pk]), {"q": "Wholesale"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 1)
        archived = self.client.patch(
            reverse("store-mail-detail", args=[self.store.pk, conversation.public_id]),
            {"action": "archive"}, format="json",
        )
        self.assertEqual(archived.data["status"], "archived")

    @override_settings(
        CLOUDFLARE_EMAIL_ROUTING_TOKEN="routing-token",
        CLOUDFLARE_ZONE_ID="a" * 32,
        CLOUDFLARE_EMAIL_WORKER_NAME="tijarago-store-mail",
    )
    @patch("store_mail.providers._request")
    @patch("store_mail.providers._rule_for_alias", return_value=None)
    def test_enable_mailbox_provisions_worker_rule(self, _rule, provider_request):
        provider_request.return_value = {"result": {"id": "rule-id"}}
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-summary", args=[self.store.pk]), {"action": "enable"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["enabled"])
        payload = provider_request.call_args.kwargs["payload"]
        self.assertEqual(payload["actions"], [{"type": "worker", "value": ["tijarago-store-mail"]}])

    @override_settings(
        CLOUDFLARE_EMAIL_ROUTING_TOKEN="routing-token",
        CLOUDFLARE_ZONE_ID="a" * 32,
        CLOUDFLARE_EMAIL_WORKER_NAME="tijarago-store-mail",
    )
    @patch("store_mail.providers._request")
    @patch("store_mail.providers._rule_for_alias")
    def test_enable_mailbox_reclaims_orphaned_legacy_rule(self, rule, provider_request):
        rule.return_value = {"id": "legacy-rule", "name": "TijaraGO store 999999"}
        provider_request.return_value = {"result": {"id": "legacy-rule"}}

        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-summary", args=[self.store.pk]), {"action": "enable"}, format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            provider_request.call_args.args[:2],
            ("PUT", f"/zones/{'a' * 32}/email/routing/rules/legacy-rule"),
        )
        self.assertEqual(provider_request.call_args.kwargs["payload"]["name"], f"TijaraGO mailbox {self.store.pk}")

    @override_settings(
        CLOUDFLARE_EMAIL_ROUTING_TOKEN="routing-token",
        CLOUDFLARE_ZONE_ID="a" * 32,
        CLOUDFLARE_EMAIL_WORKER_NAME="tijarago-store-mail",
    )
    @patch("store_mail.providers._request")
    @patch("store_mail.providers._rule_for_alias")
    def test_enable_mailbox_preserves_rule_owned_by_active_store(self, rule, provider_request):
        rule.return_value = {"id": "active-rule", "name": f"TijaraGO store {self.other_store.pk}"}
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            reverse("store-mail-summary", args=[self.store.pk]), {"action": "enable"}, format="json",
        )

        self.assertEqual(response.status_code, 502)
        provider_request.assert_not_called()

    def test_attachment_download_is_store_scoped(self):
        payload = {
            "from": "Ada <ada@example.test>", "to": "teststore@mail.tijarago.com",
            "subject": "Document", "text": "Attached", "message_id": "<attachment@example.test>",
            "attachments": [{
                "filename": "note.txt", "content_type": "text/plain",
                "content": base64.b64encode(b"private note").decode(),
            }],
        }
        body = json.dumps(payload).encode()
        self.client.post(reverse("cloudflare-mail-inbound"), body, content_type="application/json", **signed(body))
        attachment = MailMessage.objects.get().attachments.get()
        url = reverse("store-mail-attachment", args=[self.store.pk, attachment.pk])
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_authenticate(self.owner)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"private note")

    def test_resend_webhook_updates_delivery_status(self):
        self.inbound()
        conversation = MailConversation.objects.get()
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-reply", args=[self.store.pk, conversation.public_id]),
            {"text": "Reply"}, format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        sent = response.data
        payload = {
            "type": "email.delivered", "created_at": "2026-10-17T12:00:00Z",
            "data": {"email_id": "resend-id", "message_id": sent["message_id"]},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        svix_id = "msg_test"
        digest = base64.b64encode(
            hmac.new(b"webhook-secret", f"{svix_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
        ).decode()
        response = self.client.post(
            reverse("resend-mail-events"), body, content_type="application/json",
            HTTP_SVIX_ID=svix_id, HTTP_SVIX_TIMESTAMP=timestamp, HTTP_SVIX_SIGNATURE=f"v1,{digest}",
        )
        self.assertEqual(response.status_code, 200)
        message = MailMessage.objects.get(public_id=sent["public_id"])
        self.assertEqual(message.status, MailMessage.Status.DELIVERED)
        self.assertEqual(message.provider_id, "resend-id")

    def test_bounce_event_suppresses_active_marketing_subscriber(self):
        subscriber = Subscriber.objects.create(store=self.store, email="customer@example.test")
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            reverse("store-mail-compose", args=[self.store.pk]),
            {"to": subscriber.email, "subject": "Campaign", "text": "Body"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        applied = apply_resend_event({
            "type": "email.bounced",
            "created_at": "2026-10-17T12:00:00Z",
            "data": {"message_id": response.data["message_id"]},
        })
        self.assertTrue(applied)
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, Subscriber.Status.BOUNCED)
