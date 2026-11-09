"""Durable storefront job dispatch for local Django and Vercel Functions.

Vercel owns the production queue and invokes ``api/storefront-queue.ts``.
Local development starts the same idempotent executor in a detached management
command, with either cloud delivery or the explicit SQLite/Vite local mode.
The browser request is never held open by provider work.
"""

import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta

import requests
from django.db import close_old_connections
from django.db import transaction
from django.utils import timezone

from .deployment_state import ensure_initial_deployment_event, record_deployment_state
from .models import StorefrontRequest
from .repository_guards import migration_in_progress
from .services import refresh_deployment_status, run_cloud_pipeline, sync_categories_from_store

logger = logging.getLogger(__name__)
QUEUE_TOPIC = "storefront-deployments"
STARTUP_TIMEOUT = timedelta(minutes=6)
LOCAL_RECONCILIATION_INTERVAL_SECONDS = 20
LOCAL_RECONCILIATION_MAX_ATTEMPTS = 90


class DeploymentAlreadyRunning(RuntimeError):
    """A valid delivery already owns the short database execution lease."""


def vercel_oidc_token(request) -> str:
    """Return the per-invocation Vercel token without persisting it anywhere."""
    return request.headers.get("X-Vercel-OIDC-Token", "").strip()


def _is_vercel_runtime() -> bool:
    return os.environ.get("VERCEL", "").strip() == "1"


def _launch_local_job(request_id: int, task_id: uuid.UUID) -> bool:
    """Start one local worker without tying it to Django's response process.

    This is intentionally a process, not an in-process/daemon thread. It keeps
    local development responsive, survives an autoreloader request worker
    restart, and exercises the exact same database-backed executor used by the
    Vercel queue consumer.
    """
    from django.conf import settings

    manage_py = settings.BASE_DIR / "manage.py"
    subprocess.Popen(
        [
            sys.executable,
            str(manage_py),
            "execute_storefront_deployment",
            str(request_id),
            str(task_id),
        ],
        cwd=str(settings.BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return True


def _publish_vercel_job(request_id: int, task_id: uuid.UUID, oidc_token: str) -> bool:
    region = os.environ.get("VERCEL_REGION", "iad1").strip() or "iad1"
    headers = {
        "Authorization": f"Bearer {oidc_token}",
        "Content-Type": "application/json",
        "Vqs-Idempotency-Key": str(task_id),
        "Vqs-Retention-Seconds": "86400",
    }
    deployment_id = os.environ.get("VERCEL_DEPLOYMENT_ID", "").strip()
    if deployment_id:
        headers["Vqs-Deployment-Id"] = deployment_id
    response = requests.post(
        f"https://{region}.vercel-queue.com/api/v3/topic/{QUEUE_TOPIC}",
        headers=headers,
        json={
            "action": "deploy",
            "requestId": request_id,
            "taskId": str(task_id),
            "reconciliationAttempt": 0,
        },
        timeout=15,
    )
    if response.status_code not in (201, 202):
        raise RuntimeError(f"Vercel Queue rejected the job with HTTP {response.status_code}.")
    return True


def enqueue_storefront_deployment(request_id, *, force=False, oidc_token=""):
    """Create one job identity, then use Vercel Queue or a local worker process.

    Queue publication happens only after the store transaction commits. A
    serverless request must never run the long deployment inline: publication
    failures become explicit, retryable failures instead of HTTP timeouts.
    """
    with transaction.atomic():
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        if migration_in_progress(request_id):
            return False
        ensure_initial_deployment_event(request_id)
        if req.pipeline_state == StorefrontRequest.PIPELINE_LIVE:
            return False
        if req.task_id and not force:
            return False
        task_id = uuid.uuid4()
        req.task_id = task_id
        req.save(update_fields=["task_id"])

    if _is_vercel_runtime():
        try:
            if not oidc_token:
                raise RuntimeError("The deployment queue capability is unavailable.")
            return _publish_vercel_job(request_id, task_id, oidc_token)
        except Exception as exc:
            logger.exception("Could not publish storefront deployment %s.", request_id)
            record_deployment_state(
                request_id,
                StorefrontRequest.PIPELINE_FAILED,
                message="deployment.failed",
                internal_error=str(exc),
                expected_task_id=task_id,
            )
            return False

    try:
        return _launch_local_job(request_id, task_id)
    except Exception as exc:
        logger.exception("Could not start local storefront deployment %s.", request_id)
        record_deployment_state(
            request_id,
            StorefrontRequest.PIPELINE_FAILED,
            message="deployment.failed",
            internal_error=str(exc),
            expected_task_id=task_id,
        )
        return False


def fail_stalled_storefront_deployment(request_id, *, now=None) -> bool:
    """Turn a silently undelivered accepted job into a safe, retryable failure.

    A real executor increments ``attempt_count`` before doing provider work. If
    an accepted job has no attempt after the startup window, its queue delivery
    never reached Django. Reconciliation may call this while a merchant polls
    or explicitly retries; the row lock prevents a concurrent executor from
    being misclassified while it is claiming the job.
    """
    now = now or timezone.now()
    with transaction.atomic():
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        heartbeat = req.last_heartbeat_at or req.created_at
        active_pre_provider_states = {
            StorefrontRequest.PIPELINE_PREPARING,
            StorefrontRequest.PIPELINE_PUBLISHING,
            StorefrontRequest.PIPELINE_PROVISIONING,
            StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
        }
        unclaimed = (
            req.pipeline_state == StorefrontRequest.PIPELINE_ACCEPTED
            and req.attempt_count == 0
        )
        abandoned_worker = (
            req.pipeline_state in active_pre_provider_states
            and not req.vercel_deployment_id
        )
        if not heartbeat or heartbeat > now - STARTUP_TIMEOUT or not (unclaimed or abandoned_worker):
            return False
        kwargs = {"expected_task_id": req.task_id} if req.task_id else {}
        record_deployment_state(
            request_id,
            StorefrontRequest.PIPELINE_FAILED,
            message="deployment.failed",
            internal_error=(
                "The deployment worker stopped before provider handoff."
                if abandoned_worker
                else "The deployment worker did not claim the queued job within the startup window."
            ),
            **kwargs,
        )
    return True


def execute_storefront_deployment(request_id, *, task_id):
    """Execute one at-least-once delivery with a database-backed lease.

    The task UUID prevents an older delivery from overwriting a newer retry.
    Active state plus a fresh heartbeat acts as a lease, preventing concurrent
    provider mutations without holding a database transaction during network
    calls. A killed function becomes retryable after the queue visibility
    window.
    """
    task_id = uuid.UUID(str(task_id))
    active_states = {
        StorefrontRequest.PIPELINE_PREPARING,
        StorefrontRequest.PIPELINE_PUBLISHING,
        StorefrontRequest.PIPELINE_PROVISIONING,
        StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
    }
    lease_cutoff = timezone.now() - timedelta(seconds=330)

    with transaction.atomic():
        # Lock only the deployment row. Joining the nullable Store relations in
        # this query makes PostgreSQL reject FOR UPDATE on the outer join.
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        if migration_in_progress(request_id):
            return {"status": "migration_active", "terminal": True}
        if req.pipeline_state == StorefrontRequest.PIPELINE_LIVE:
            return {"status": StorefrontRequest.PIPELINE_LIVE, "terminal": True}
        if req.task_id != task_id:
            return {"status": "superseded", "terminal": True}
        if (
            req.pipeline_state in active_states
            and req.last_heartbeat_at
            and req.last_heartbeat_at > lease_cutoff
        ):
            raise DeploymentAlreadyRunning("This deployment is already being processed.")
        req.attempt_count += 1
        req.save(update_fields=["attempt_count"])
        record_deployment_state(
            request_id,
            StorefrontRequest.PIPELINE_PREPARING,
            message="deployment.preparing",
            expected_task_id=task_id,
        )

    try:
        req = StorefrontRequest.objects.select_related("store").get(pk=request_id)
        sync_categories_from_store(req)
        pipeline = run_cloud_pipeline
        from django.conf import settings
        if settings.STOREFRONT_DEPLOYMENT_MODE == 'local':
            from .local_preview import run_local_pipeline
            pipeline = run_local_pipeline
        pipeline(
            req,
            on_state=lambda state, progress, message: record_deployment_state(
                request_id,
                state,
                progress=progress,
                message=message,
                expected_task_id=task_id,
            ),
        )
        req.refresh_from_db()
        refresh_deployment_status(req, expected_task_id=task_id)
        req.refresh_from_db()
        return {
            "status": req.pipeline_state,
            "terminal": req.pipeline_state in {
                StorefrontRequest.PIPELINE_LIVE,
                StorefrontRequest.PIPELINE_FAILED,
            },
        }
    except Exception as exc:
        logger.exception("Storefront deployment %s failed", request_id)
        StorefrontRequest.objects.filter(pk=request_id, task_id=task_id).update(
            log=f"Pipeline error: {exc}",
            deploy_log=f"Pipeline error: {exc}",
        )
        if StorefrontRequest.objects.filter(pk=request_id, task_id=task_id).exists():
            record_deployment_state(
                request_id,
                StorefrontRequest.PIPELINE_FAILED,
                message="deployment.failed",
                internal_error=str(exc),
                expected_task_id=task_id,
            )
        else:
            return {"status": "superseded", "terminal": True}
        return {"status": StorefrontRequest.PIPELINE_FAILED, "terminal": True}


def reconcile_active_storefront_deployments(limit=100):
    """Refresh provider truth for active builds; safe for polling or cron use."""
    request_ids = list(
        StorefrontRequest.objects.filter(
            pipeline_state__in=[
                StorefrontRequest.PIPELINE_BUILDING,
                StorefrontRequest.PIPELINE_VERIFYING,
            ],
        )
        .exclude(vercel_deployment_id="")
        .values_list("id", flat=True)[:limit]
    )
    for request_id in request_ids:
        req = StorefrontRequest.objects.get(pk=request_id)
        refresh_deployment_status(req)
    return len(request_ids)


def reconcile_storefront_deployment(request_id, *, task_id, attempt=1):
    """Reconcile one exact queue job without allowing stale jobs to mutate it."""
    task_id = uuid.UUID(str(task_id))
    req = StorefrontRequest.objects.filter(pk=request_id, task_id=task_id).first()
    if req is None:
        return {"status": "superseded", "terminal": True}
    if req.pipeline_state in {
        StorefrontRequest.PIPELINE_LIVE,
        StorefrontRequest.PIPELINE_FAILED,
    }:
        return {"status": req.pipeline_state, "terminal": True}

    refresh_deployment_status(req, expected_task_id=task_id)
    req.refresh_from_db()
    if req.pipeline_state in {
        StorefrontRequest.PIPELINE_LIVE,
        StorefrontRequest.PIPELINE_FAILED,
    }:
        return {"status": req.pipeline_state, "terminal": True}
    if attempt >= 90:
        record_deployment_state(
            request_id,
            StorefrontRequest.PIPELINE_FAILED,
            message="deployment.timeout",
            internal_error="Deployment did not become publicly reachable within 30 minutes.",
            expected_task_id=task_id,
        )
        return {"status": StorefrontRequest.PIPELINE_FAILED, "terminal": True}
    return {"status": req.pipeline_state, "terminal": False}


def execute_and_reconcile_local_deployment(request_id, *, task_id, sleeper=time.sleep):
    """Run a detached local job through provider and public-health completion.

    Production uses delayed Vercel Queue messages for reconciliation. Local
    development has no queue consumer, so its detached worker owns the same
    bounded reconciliation loop. Every check is capability-scoped to the
    current task id and therefore cannot complete a superseded retry.
    """
    result = execute_storefront_deployment(request_id, task_id=task_id)
    if result["terminal"]:
        return result
    for attempt in range(1, LOCAL_RECONCILIATION_MAX_ATTEMPTS + 1):
        sleeper(LOCAL_RECONCILIATION_INTERVAL_SECONDS)
        close_old_connections()
        result = reconcile_storefront_deployment(
            request_id,
            task_id=task_id,
            attempt=attempt,
        )
        if result["terminal"]:
            return result
    return result
