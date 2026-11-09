"""Phase 2 showroom API.

Designs (public):  GET /api/designs/          GET /api/designs/<key>/
Requests (merchant): GET/POST /api/requests/  GET /api/requests/<id>/
                     POST /api/requests/<id>/scaffold
"""
import asyncio
import json
import time
from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.handlers.asgi import ASGIRequest
from django.db import close_old_connections, transaction
from django.http import StreamingHttpResponse

from rest_framework import generics
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.renderers import BaseRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from store.access import MANAGER_ROLES, is_store_member
from store.design_access import can_use_design
from storefront_factory.models import StorefrontDeploymentEvent, StorefrontRequest
from storefront_factory.repository_guards import migration_in_progress
from storefront_factory.deployment_state import record_deployment_state
from storefront_factory.deployment_state import ensure_initial_deployment_event
from storefront_factory.services import (
    refresh_deployment_status,
)
from storefront_factory.jobs import (
    enqueue_storefront_deployment,
    fail_stalled_storefront_deployment,
    vercel_oidc_token,
)

from .designs import DesignNotFound, get_design, list_designs
from .serializers import DesignSerializer, StorefrontRequestSerializer


class DeploymentConflict(APIException):
    status_code = 409
    default_detail = "This storefront is already being prepared."
    default_code = "storefront_deployment_active"


class EventStreamRenderer(BaseRenderer):
    """Content-negotiation marker for browser EventSource requests.

    The view returns a StreamingHttpResponse directly, so DRF does not render
    the event body. This renderer exists to explicitly accept the browser's
    ``Accept: text/event-stream`` header instead of rejecting it as JSON-only.
    """

    media_type = "text/event-stream"
    format = "event-stream"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode(self.charset)
        return json.dumps(data, separators=(",", ":")).encode(self.charset)


def _require_member(user, store):
    if not is_store_member(user, store, MANAGER_ROLES):
        raise PermissionDenied("Only store owners or admins can manage storefront deployments.")


def _get_request(pk, user):
    req = generics.get_object_or_404(
        StorefrontRequest.objects.select_related("store").filter(
            store__members__user=user,
            store__members__role__in=MANAGER_ROLES,
        ),
        pk=pk,
    )
    return req


def _deployment_events_after(request_id, last_event_id):
    """Read one event batch without retaining a request-scoped DB connection."""
    close_old_connections()
    try:
        return list(
            StorefrontDeploymentEvent.objects.filter(
                storefront_id=request_id,
                version__gt=last_event_id,
            ).order_by("version")[:100]
        )
    finally:
        close_old_connections()


def _reconcile_deployment_truth(request_id):
    """Refresh provider/public readiness without retaining an SSE DB connection.

    Queue delivery is deliberately not the only source of truth. A serverless
    follow-up can be delayed or lost after the hosting provider has already
    made the storefront public, so every active progress stream periodically
    reconciles the persisted state as well.
    """
    close_old_connections()
    try:
        deployment = StorefrontRequest.objects.get(pk=request_id)
        refresh_deployment_status(deployment)
    finally:
        close_old_connections()


def _deployment_event_chunk(event):
    payload = {
        "version": event.version,
        "state": event.state,
        "progress": event.progress,
        "message": event.message,
        "request": event.snapshot,
    }
    return (
        f"id: {event.version}\n"
        "event: deployment.updated\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    )


# ── Designs (public) ────────────────────────────────────────────────

class DesignListView(APIView):
    """GET /api/designs/ — the design catalog (design-stock/manifest.json)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        from theme_previewer.registry import preview_payload

        return Response(DesignSerializer([preview_payload(item) for item in list_designs()], many=True).data)


class DesignDetailView(APIView):
    """GET /api/designs/<key>/ — one design, incl. its config_schema."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, key):
        try:
            design = get_design(key)
        except DesignNotFound:
            raise NotFound("Design not found.")
        from theme_previewer.registry import preview_payload

        return Response(DesignSerializer(preview_payload(design)).data)


# ── Deploy requests (merchant) ──────────────────────────────────────

class RequestListCreateView(generics.ListCreateAPIView):
    """GET  → the merchant's deploy requests (across their stores).
    POST → create/replace a deploy request for a store (store + design +
    config). The request's slug is the store's slug; one request per store."""
    serializer_class = StorefrontRequestSerializer
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "storefront_configuration"
            self.throttle_classes = [ScopedRateThrottle]
        return super().get_throttles()

    def get_queryset(self):
        return StorefrontRequest.objects.filter(
            store__members__user=self.request.user,
            store__members__role__in=MANAGER_ROLES,
        ).select_related("store")

    def perform_create(self, serializer):
        store = serializer.validated_data["store"]
        _require_member(self.request.user, store)
        with transaction.atomic():
            deployment = StorefrontRequest.objects.select_for_update().filter(store=store).first()
            if deployment and migration_in_progress(deployment.pk):
                raise DeploymentConflict("This storefront's repository migration must finish first.")
            active_states = {
                StorefrontRequest.PIPELINE_PREPARING,
                StorefrontRequest.PIPELINE_PUBLISHING,
                StorefrontRequest.PIPELINE_PROVISIONING,
                StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
                StorefrontRequest.PIPELINE_BUILDING,
                StorefrontRequest.PIPELINE_VERIFYING,
            }
            if deployment and (
                deployment.pipeline_state in active_states
                or (
                    deployment.pipeline_state == StorefrontRequest.PIPELINE_ACCEPTED
                    and deployment.status != StorefrontRequest.STATUS_PENDING
                )
            ):
                raise DeploymentConflict()
            if deployment:
                serializer.replace(deployment)
            else:
                serializer.save()


class RequestDetailView(APIView):
    """GET /api/requests/<id>/ — poll scaffold/deploy status."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        req = _get_request(pk, request.user)
        if fail_stalled_storefront_deployment(req.pk):
            req.refresh_from_db()
        refresh_deployment_status(req)
        return Response(StorefrontRequestSerializer(req).data)


class RequestScaffoldView(APIView):
    """POST /api/requests/<id>/scaffold — dispatch the configured delivery.

    Cloud mode publishes source and provisions hosting/domain. Opt-in local
    mode serves the design from the local checkout against SQLite. Both report
    progress and a URL through the same request/status/event API.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_deployment"

    def post(self, request, pk):
        req = _get_request(pk, request.user)
        if fail_stalled_storefront_deployment(req.pk):
            req.refresh_from_db()
        if req.store_id:
            from subscriptions.services import sync_store_entitlement
            sync_store_entitlement(req.store)
            req.store.config.refresh_from_db(fields=["plan"])
        if req.store_id and not can_use_design(req.store.config.plan, req.design):
            raise PermissionDenied("This premium design requires an active Premium plan.")
        with transaction.atomic():
            req = StorefrontRequest.objects.select_for_update().get(pk=req.pk)
            if migration_in_progress(req.pk):
                raise DeploymentConflict("This storefront's repository migration must finish first.")
            local_restart = (
                settings.STOREFRONT_DEPLOYMENT_MODE == "local"
                and req.pipeline_state == StorefrontRequest.PIPELINE_LIVE
                and urlparse(req.deployed_url).hostname in {"localhost", "127.0.0.1"}
                and not req.vercel_project_id
                and not req.vercel_deployment_id
            )
            if (
                req.status not in (
                    StorefrontRequest.STATUS_PENDING,
                    StorefrontRequest.STATUS_FAILED,
                )
                and req.pipeline_state != StorefrontRequest.PIPELINE_FAILED
                and not local_restart
            ):
                return Response(
                    {"code": "storefront_deployment_active", "detail": "This storefront is already being prepared."},
                    status=409,
                )
            reset_values = dict(
                task_id=None,
                attempt_count=0,
                log="",
                deploy_log="",
                app_path="",
                port=None,
                github_commit_sha="",
                vercel_deployment_id="",
                started_at=None,
                last_heartbeat_at=None,
                completed_at=None,
                last_known_good_revision=req.last_known_good_revision,
                last_known_good_deployment_id=req.last_known_good_deployment_id,
            )
            if not req.last_known_good_revision:
                reset_values["deployed_url"] = ""
            StorefrontRequest.objects.filter(pk=req.pk).update(**reset_values)
            req = record_deployment_state(
                req.pk,
                StorefrontRequest.PIPELINE_ACCEPTED,
                message="deployment.accepted",
                reset=True,
            )
            queue_token = vercel_oidc_token(request)
            transaction.on_commit(
                lambda token=queue_token: enqueue_storefront_deployment(
                    req.pk,
                    force=True,
                    oidc_token=token,
                )
            )

        return Response(StorefrontRequestSerializer(req).data, status=202)


class RequestEventsView(APIView):
    """Short-lived SSE stream; EventSource reconnects using Last-Event-ID."""

    permission_classes = [IsAuthenticated]
    renderer_classes = [EventStreamRenderer]

    def get(self, request, pk):
        req = _get_request(pk, request.user)
        supplied_event_id = (
            request.headers.get("Last-Event-ID")
            or request.query_params.get("after")
        )
        try:
            # A new EventSource should receive the current attempt's latest
            # state, not the full immutable history. This matters after a
            # retry, where replaying an earlier FAILED event would make the
            # showroom stop monitoring the healthy replacement attempt.
            last_event_id = max(0, int(supplied_event_id)) if supplied_event_id else max(
                0,
                req.event_version - 1,
            )
        except (TypeError, ValueError):
            last_event_id = max(0, req.event_version - 1)
        single_pass = request.query_params.get("once") == "1"

        def sync_stream():
            nonlocal last_event_id
            cycles = 0
            while cycles < 25:
                events = _deployment_events_after(req.pk, last_event_id)
                for event in events:
                    last_event_id = event.version
                    yield _deployment_event_chunk(event)
                if cycles % 5 == 0:
                    _reconcile_deployment_truth(req.pk)
                if single_pass:
                    break
                if not events and cycles % 10 == 0:
                    yield ": keep-alive\n\n"
                cycles += 1
                time.sleep(1)

        async def async_stream():
            nonlocal last_event_id
            cycles = 0
            while cycles < 25:
                events = await sync_to_async(
                    _deployment_events_after,
                    thread_sensitive=True,
                )(req.pk, last_event_id)
                for event in events:
                    last_event_id = event.version
                    yield _deployment_event_chunk(event)
                if cycles % 5 == 0:
                    await sync_to_async(
                        _reconcile_deployment_truth,
                        thread_sensitive=True,
                    )(req.pk)
                if single_pass:
                    break
                if not events and cycles % 10 == 0:
                    yield ": keep-alive\n\n"
                cycles += 1
                await asyncio.sleep(1)

        django_request = getattr(request, "_request", request)
        stream = async_stream() if isinstance(django_request, ASGIRequest) else sync_stream()
        response = StreamingHttpResponse(stream, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response
