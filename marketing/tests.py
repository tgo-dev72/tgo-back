"""Tenant-scoped newsletter tests. Django swaps in the locmem email backend
during tests, so mail.outbox captures everything send_campaign emits."""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from unittest.mock import patch

from store.models import Store, StoreConfig, StoreMember

from .emails import prepare_campaign_deliveries, send_campaign, send_campaign_batch
from .models import CampaignDelivery, NewsletterCampaign, Subscriber


def make_store(slug="news"):
    store, _ = Store.objects.get_or_create(slug=slug, defaults={"name": f"{slug} store"})
    StoreConfig.objects.update_or_create(
        store=store, defaults={"from_email": f"newsletter@{slug}.example.com", "brand_color": "#123456"}
    )
    return store


class SubscribeTests(TestCase):
    def setUp(self):
        self.store = make_store()
        self.client = APIClient()

    def test_subscribe(self):
        res = self.client.post(reverse("subscribe"), {"email": "a@b.co"}, format="json",
                               HTTP_X_STORE_SLUG=self.store.slug)
        self.assertEqual(res.status_code, 201, res.data)
        sub = Subscriber.objects.get(store=self.store, email="a@b.co")
        self.assertEqual(sub.status, Subscriber.Status.ACTIVE)

    def test_duplicate_is_idempotent(self):
        for _ in range(2):
            res = self.client.post(reverse("subscribe"), {"email": "a@b.co"}, format="json",
                                   HTTP_X_STORE_SLUG=self.store.slug)
        self.assertEqual(Subscriber.objects.count(), 1)

    def test_same_email_two_stores_is_allowed(self):
        store2 = make_store("news2")
        payload = {"email": "a@b.co"}
        self.client.post(reverse("subscribe"), payload, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        self.client.post(reverse("subscribe"), payload, format="json", HTTP_X_STORE_SLUG=store2.slug)
        self.assertEqual(Subscriber.objects.count(), 2)

    def test_resubscribe_after_unsubscribe(self):
        self.client.post(reverse("subscribe"), {"email": "a@b.co"}, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        self.client.post(reverse("unsubscribe"), {"email": "a@b.co"}, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        sub = Subscriber.objects.get(store=self.store, email="a@b.co")
        self.assertEqual(sub.status, Subscriber.Status.UNSUBSCRIBED)
        res = self.client.post(reverse("subscribe"), {"email": "a@b.co"}, format="json", HTTP_X_STORE_SLUG=self.store.slug)
        self.assertIn("resubscribed", res.data["detail"])
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscriber.Status.ACTIVE)

    def test_unknown_store_404(self):
        res = self.client.post(reverse("subscribe"), {"email": "a@b.co"}, format="json", HTTP_X_STORE_SLUG="nope")
        self.assertEqual(res.status_code, 404)

    @override_settings(
        STOREFRONT_API_URL="https://api.example.test",
        PLATFORM_WEBSITE_URL="https://www.example.test",
        UNSUBSCRIBE_BASE_URL="",
    )
    def test_campaign_unsubscribe_link_uses_branded_opaque_url_and_one_click_endpoint(self):
        subscriber = Subscriber.objects.create(store=self.store, email="signed@example.com")
        campaign = NewsletterCampaign.objects.create(store=self.store, subject="Hello", body_html="<p>Hi</p>")
        send_campaign(campaign, test_email=subscriber.email)
        one_click_link = mail.outbox[0].extra_headers["List-Unsubscribe"].strip("<>")
        public_link = f"https://www.example.test/unsubscribe/{subscriber.pk}/?lang=en"
        self.assertEqual(one_click_link, f"https://www.example.test/api/unsubscribe/{subscriber.pk}")
        self.assertIn(public_link, mail.outbox[0].alternatives[0].content)
        self.assertNotIn(subscriber.email, public_link)
        confirm = self.client.get(reverse("unsubscribe-opaque", args=[subscriber.pk]))
        self.assertEqual(confirm.status_code, 200)
        self.assertContains(confirm, "Confirm unsubscribe")
        result = self.client.post(reverse("unsubscribe-opaque", args=[subscriber.pk]), {"List-Unsubscribe": "One-Click"})
        self.assertEqual(result.status_code, 200, result.data)
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, Subscriber.Status.UNSUBSCRIBED)

    def test_invalid_signed_unsubscribe_link_is_rejected(self):
        response = self.client.post(reverse("unsubscribe") + "?token=invalid", {})
        self.assertEqual(response.status_code, 400)


class CampaignTests(TestCase):
    def setUp(self):
        self.store = make_store()
        Subscriber.objects.create(store=self.store, email="one@x.com")
        Subscriber.objects.create(store=self.store, email="two@x.com")
        self.other = make_store("news-other")
        Subscriber.objects.create(store=self.other, email="other@x.com")
        self.owner = get_user_model().objects.create_user(email="owner@x.com", password="pw")
        StoreMember.objects.create(user=self.owner, store=self.store, role=StoreMember.Roles.OWNER)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _create_campaign(self, **kw):
        data = {"subject": "Hello", "preview_text": "p", "body_html": "<p>Hi</p>", "body_text": "Hi", **kw}
        return self.client.post(reverse("campaign-list", args=[self.store.id]), data, format="json")

    def test_create_and_send_to_store_subscribers_only(self):
        res = self._create_campaign()
        self.assertEqual(res.status_code, 201, res.data)
        campaign = NewsletterCampaign.objects.get(pk=res.data["id"])

        summary = send_campaign(campaign)
        self.assertEqual(summary["sent"], 2)
        self.assertEqual(len(mail.outbox), 2)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {"one@x.com", "two@x.com"})  # other store's subscriber untouched
        self.assertIn("newsletter@news.example.com", mail.outbox[0].from_email)

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, NewsletterCampaign.Status.SENT)
        self.assertEqual(campaign.sent_count, 2)
        subscriber_ids = {str(value) for value in Subscriber.objects.filter(store=self.store).values_list("pk", flat=True)}
        self.assertTrue(any(value in mail.outbox[0].extra_headers["List-Unsubscribe"] for value in subscriber_ids))
        self.assertNotIn(self.store.slug, mail.outbox[0].extra_headers["List-Unsubscribe"])

    def test_arabic_campaign_sets_rtl_email_and_localized_footer(self):
        res = self._create_campaign(language="ar")
        self.assertEqual(res.status_code, 201, res.data)
        campaign = NewsletterCampaign.objects.get(pk=res.data["id"])
        send_campaign(campaign, test_email="one@x.com")
        html = mail.outbox[0].alternatives[0].content
        self.assertIn('<html lang="ar" dir="rtl">', html)
        self.assertIn("إلغاء الاشتراك", html)

    def test_send_via_api(self):
        res = self._create_campaign()
        campaign_id = res.data["id"]
        res = self.client.post(reverse("campaign-send", args=[self.store.id, campaign_id]), {}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["sent"], 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_test_email_does_not_update_counters(self):
        res = self._create_campaign()
        campaign = NewsletterCampaign.objects.get(pk=res.data["id"])
        summary = send_campaign(campaign, test_email="qa@x.com")
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["qa@x.com"])
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, NewsletterCampaign.Status.DRAFT)
        self.assertIsNone(campaign.sent_at)
        self.assertEqual(campaign.sent_count, 0)

    def test_sent_campaign_cannot_be_sent_twice(self):
        res = self._create_campaign()
        campaign_id = res.data["id"]
        first = self.client.post(reverse("campaign-send", args=[self.store.id, campaign_id]), {}, format="json")
        second = self.client.post(reverse("campaign-send", args=[self.store.id, campaign_id]), {}, format="json")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 409, second.data)
        self.assertEqual(len(mail.outbox), 2)

    def test_in_flight_campaign_reserves_monthly_allowance(self):
        NewsletterCampaign.objects.create(
            store=self.store,
            subject="Already sending",
            body_html="<p>Reserved</p>",
            status=NewsletterCampaign.Status.SENDING,
            recipients_count=100,
        )
        res = self._create_campaign(subject="Next")

        response = self.client.post(
            reverse("campaign-send", args=[self.store.id, res.data["id"]]),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(len(mail.outbox), 0)

    def test_non_member_cannot_create_or_send(self):
        outsider = get_user_model().objects.create_user(email="outsider@x.com", password="pw")
        client = APIClient()
        client.force_authenticate(outsider)
        res = client.get(reverse("campaign-list", args=[self.store.id]))
        self.assertEqual(res.status_code, 404)

    def test_empty_subscriber_list(self):
        empty_store = make_store("news-empty")
        empty_owner = get_user_model().objects.create_user(email="e@x.com", password="pw")
        StoreMember.objects.create(user=empty_owner, store=empty_store, role=StoreMember.Roles.OWNER)
        client = APIClient()
        client.force_authenticate(empty_owner)
        res = client.post(reverse("campaign-list", args=[empty_store.id]),
                          {"subject": "S", "body_html": "<p>x</p>"}, format="json")
        campaign = NewsletterCampaign.objects.get(pk=res.data["id"])
        summary = send_campaign(campaign)
        self.assertEqual(summary["error"], "No active subscribers.")
        self.assertEqual(len(mail.outbox), 0)

    def test_batched_delivery_is_resumable_and_updates_campaign_totals(self):
        campaign = NewsletterCampaign.objects.create(
            store=self.store, subject="Batch", body_html="<p>Batch</p>", body_text="Batch",
        )
        task_id, total = prepare_campaign_deliveries(campaign)
        self.assertEqual(total, 2)
        first = send_campaign_batch(campaign.pk, task_id=task_id, batch_size=1)
        self.assertFalse(first["terminal"])
        self.assertEqual(first["sent"], 1)
        second = send_campaign_batch(campaign.pk, task_id=task_id, batch_size=1)
        self.assertTrue(second["terminal"])
        self.assertEqual(second["sent"], 2)
        self.assertEqual(CampaignDelivery.objects.filter(status=CampaignDelivery.Status.SENT).count(), 2)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, NewsletterCampaign.Status.SENT)

    @patch.dict("os.environ", {"VERCEL": "1"})
    @patch("marketing.views.enqueue_campaign_delivery", return_value=True)
    def test_production_send_queues_recipient_ledger(self, enqueue):
        campaign_id = self._create_campaign().data["id"]
        response = self.client.post(
            reverse("campaign-send", args=[self.store.id, campaign_id]),
            {}, format="json", HTTP_X_VERCEL_OIDC_TOKEN="oidc",
        )
        self.assertEqual(response.status_code, 202, response.data)
        campaign = NewsletterCampaign.objects.get(pk=campaign_id)
        self.assertEqual(campaign.deliveries.count(), 2)
        enqueue.assert_called_once_with(campaign.pk, campaign.delivery_task_id, oidc_token="oidc")
