from datetime import timedelta
from html import escape

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from access_control.services import permitted_store
from store.context import resolve_store

from .emails import UNSUBSCRIBE_SIGNING_SALT, prepare_campaign_deliveries, send_campaign
from .jobs import enqueue_campaign_delivery, production_queue_enabled
from .models import NewsletterCampaign, Subscriber
from .serializers import ContactInquirySerializer, NewsletterCampaignSerializer, SubscribeSerializer


class SubscribeView(APIView):
    """POST /api/subscribe/ — public storefront newsletter signup."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        store = resolve_store(request)
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower().strip()

        subscriber, created = Subscriber.objects.get_or_create(
            store=store, email=email, defaults={"status": Subscriber.Status.ACTIVE}
        )
        if not created and subscriber.status == Subscriber.Status.UNSUBSCRIBED:
            subscriber.status = Subscriber.Status.ACTIVE
            subscriber.unsubscribed_at = None
            subscriber.save(update_fields=["status", "unsubscribed_at"])
            return Response({"detail": "Welcome back! You've been resubscribed."})
        if not created:
            return Response({"detail": "You're already subscribed."})
        return Response({"detail": "Successfully subscribed!"}, status=status.HTTP_201_CREATED)


class ContactInquiryView(APIView):
    """Public, tenant-scoped contact form delivered to the merchant mailbox."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_contact"

    def post(self, request):
        from email.utils import formataddr
        from store_mail.services import ingest_inbound

        store = resolve_store(request)
        serializer = ContactInquirySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        phone = data.get("phone", "")
        body = data["message"]
        if phone:
            body = f"Phone: {phone}\n\n{body}"
        ingest_inbound(store, {
            "from": formataddr((data["name"], data["email"].lower())),
            "subject": data["subject"],
            "text": body,
        })
        return Response({"detail": "Your inquiry has been sent."}, status=status.HTTP_201_CREATED)


class UnsubscribeView(APIView):
    """Signed browser confirmation and RFC 8058 one-click unsubscribe endpoint."""
    authentication_classes = []
    permission_classes = [AllowAny]

    @staticmethod
    def _signed_target(request, code=None):
        if code:
            subscriber = Subscriber.objects.select_related("store").filter(pk=code).first()
            return (subscriber.store, subscriber.email) if subscriber else False
        token = request.data.get("token") or request.query_params.get("token")
        if not token:
            return None
        try:
            payload = signing.loads(token, salt=UNSUBSCRIBE_SIGNING_SALT)
            store_id = int(payload["store_id"])
            email = str(payload["email"]).lower().strip()
        except (signing.BadSignature, KeyError, TypeError, ValueError):
            return False
        from store.models import Store
        store = Store.objects.filter(pk=store_id).first()
        return (store, email) if store and email else False

    def get(self, request, code=None):
        target = self._signed_target(request, code)
        if not target:
            return HttpResponse("Invalid unsubscribe link.", status=400, content_type="text/plain")
        store, _email = target
        token = escape(request.query_params.get("token", ""), quote=True)
        brand = escape(store.name)
        hidden = (
            f"<input type='hidden' name='token' value='{token}'>"
            if token else ""
        )
        return HttpResponse(
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>Unsubscribe · {brand}</title></head><body style='font-family:system-ui;max-width:560px;margin:10vh auto;padding:24px'>"
            f"<h1>Unsubscribe from {brand}</h1><p>You will no longer receive marketing emails from this store.</p>"
            f"<form method='post'>{hidden}"
            "<button style='padding:12px 18px' type='submit'>Confirm unsubscribe</button></form></body></html>",
            content_type="text/html; charset=utf-8",
        )

    def post(self, request, code=None):
        target = self._signed_target(request, code)
        if target is False:
            return Response({"detail": "Invalid unsubscribe link."}, status=status.HTTP_400_BAD_REQUEST)
        if target:
            store, email = target
        else:
            store = resolve_store(request)
            email = request.data.get("email") or request.query_params.get("email")
        if not email:
            return Response({"detail": "Email is required."}, status=status.HTTP_400_BAD_REQUEST)
        email = email.lower().strip()

        subscriber = Subscriber.objects.filter(store=store, email=email).first()
        if subscriber is None:
            return Response({"detail": "Email not found."}, status=status.HTTP_404_NOT_FOUND)
        if subscriber.status != Subscriber.Status.UNSUBSCRIBED:
            from django.utils import timezone

            subscriber.status = Subscriber.Status.UNSUBSCRIBED
            subscriber.unsubscribed_at = timezone.now()
            subscriber.save(update_fields=["status", "unsubscribed_at"])
        return Response({"detail": "You've been unsubscribed."})


class CampaignListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/store/<id>/campaigns/ — merchant newsletter campaigns."""
    serializer_class = NewsletterCampaignSerializer
    permission_classes = [IsAuthenticated]

    def get_store(self):
        permission = "marketing.view" if self.request.method == "GET" else "marketing.manage"
        return permitted_store(self.request.user, self.kwargs["store_id"], permission)

    def get_queryset(self):
        return NewsletterCampaign.objects.filter(store=self.get_store())

    def perform_create(self, serializer):
        serializer.save(store=self.get_store())


class CampaignSendView(APIView):
    """POST /api/store/<id>/campaigns/<pk>/send/ — merchant sends a campaign."""
    permission_classes = [IsAuthenticated]

    def post(self, request, store_id, pk):
        store = permitted_store(request.user, store_id, "marketing.manage")
        test_email = request.data.get("test_email")
        if test_email:
            email_serializer = SubscribeSerializer(data={"email": test_email})
            email_serializer.is_valid(raise_exception=True)
            campaign = generics.get_object_or_404(NewsletterCampaign.objects.filter(store=store), pk=pk)
            summary = send_campaign(campaign, test_email=email_serializer.validated_data["email"])
            return Response({"detail": "Test email sent.", **summary})

        from store.models import StoreMember
        from subscriptions.policies import EntitlementDenied, enforce_operation

        owner_id = store.members.get(role=StoreMember.Roles.OWNER).user_id
        with transaction.atomic():
            # Every campaign for an owner shares the same plan allowance. The
            # owner lock makes quota calculation + reservation one operation.
            owner = get_user_model().objects.select_for_update().get(pk=owner_id)
            NewsletterCampaign.objects.filter(
                store=store,
                status=NewsletterCampaign.Status.SENDING,
                updated_at__lt=timezone.now() - timedelta(hours=1),
            ).update(status=NewsletterCampaign.Status.FAILED, updated_at=timezone.now())
            campaign = generics.get_object_or_404(
                NewsletterCampaign.objects.select_for_update().filter(store=store),
                pk=pk,
            )
            if campaign.status in {NewsletterCampaign.Status.SENDING, NewsletterCampaign.Status.SENT}:
                return Response(
                    {"detail": "This campaign is already being sent or has already been sent."},
                    status=status.HTTP_409_CONFLICT,
                )
            if campaign.status == NewsletterCampaign.Status.FAILED and campaign.sent_count:
                return Response(
                    {"detail": "Some recipients already received this campaign. Duplicate it before trying again."},
                    status=status.HTTP_409_CONFLICT,
                )
            recipient_count = store.subscribers.filter(status=Subscriber.Status.ACTIVE).count()
            if not recipient_count:
                return Response({"detail": "No active subscribers.", "sent": 0, "failed": 0, "total": 0})
            try:
                enforce_operation(
                    owner,
                    "marketing.campaign.send",
                    context={"store": store, "amount": recipient_count},
                )
            except EntitlementDenied as exc:
                return Response(exc.as_dict(), status=status.HTTP_403_FORBIDDEN)
            campaign.status = NewsletterCampaign.Status.SENDING
            campaign.recipients_count = recipient_count
            campaign.save(update_fields=["status", "recipients_count", "updated_at"])

        if production_queue_enabled():
            from storefront_factory.jobs import vercel_oidc_token
            task_id, total = prepare_campaign_deliveries(campaign)
            queued = enqueue_campaign_delivery(
                campaign.pk, task_id, oidc_token=vercel_oidc_token(request),
            )
            if not queued:
                return Response(
                    {"detail": "Campaign delivery could not be queued. Try again.", "sent": 0, "failed": 0, "total": total},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response(
                {"detail": "Campaign queued.", "queued": True, "sent": 0, "failed": 0, "total": total},
                status=status.HTTP_202_ACCEPTED,
            )

        summary = send_campaign(campaign, prepared=True)
        return Response({"detail": "Campaign sent.", **summary})
