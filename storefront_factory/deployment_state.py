from django.db import transaction
from django.utils import timezone

from .models import StorefrontDeploymentEvent, StorefrontRequest


STATE_PROGRESS = {
    StorefrontRequest.PIPELINE_ACCEPTED: 0,
    StorefrontRequest.PIPELINE_PREPARING: 10,
    StorefrontRequest.PIPELINE_PUBLISHING: 35,
    StorefrontRequest.PIPELINE_PROVISIONING: 55,
    StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN: 70,
    StorefrontRequest.PIPELINE_BUILDING: 82,
    StorefrontRequest.PIPELINE_VERIFYING: 94,
    StorefrontRequest.PIPELINE_LIVE: 100,
    StorefrontRequest.PIPELINE_FAILED: 100,
}


def _safe_snapshot(req):
    return {
        "id": req.pk,
        "store": req.store_id,
        "design": req.design,
        "name": req.name,
        "slug": req.slug,
        "status": req.status,
        "deploy_status": req.deploy_status,
        "pipeline_state": req.pipeline_state,
        "progress": req.progress,
        "public_message": req.public_message,
        "event_version": req.event_version,
        "deployed_url": req.deployed_url,
        "operation": req.operation,
        "target_revision": req.target_revision,
        "deployed_revision": req.deployed_revision,
        "last_known_good_revision": req.last_known_good_revision,
        "app_path": req.app_path,
        "port": req.port,
        "log": "",
        "deploy_log": "",
        "created_at": req.created_at.isoformat() if req.created_at else "",
    }


def record_deployment_state(
    request_id,
    state,
    *,
    message="",
    progress=None,
    internal_error=None,
    reset=False,
    expected_task_id=None,
):
    """Atomically update current state and append an immutable public event."""
    if state not in dict(StorefrontRequest.PIPELINE_STATE_CHOICES):
        raise ValueError(f"Unknown storefront pipeline state: {state}")
    target_progress = STATE_PROGRESS[state] if progress is None else max(0, min(100, int(progress)))
    now = timezone.now()

    with transaction.atomic():
        req = StorefrontRequest.objects.select_for_update().get(pk=request_id)
        if expected_task_id is not None and str(req.task_id or "") != str(expected_task_id):
            return None
        if state == StorefrontRequest.PIPELINE_FAILED and progress is None:
            target_progress = req.progress
        if not reset:
            target_progress = max(req.progress, target_progress)
        req.pipeline_state = state
        req.progress = target_progress
        req.public_message = message
        req.last_heartbeat_at = now
        if state == StorefrontRequest.PIPELINE_PREPARING and req.started_at is None:
            req.started_at = now
        if state == StorefrontRequest.PIPELINE_LIVE:
            req.status = StorefrontRequest.STATUS_CREATED
            req.deploy_status = StorefrontRequest.DEPLOY_DEPLOYED
            req.completed_at = now
            req.internal_error = ""
            if req.target_revision:
                req.deployed_revision = req.target_revision
                req.last_known_good_revision = req.target_revision
            if req.vercel_deployment_id:
                req.last_known_good_deployment_id = req.vercel_deployment_id
            if req.store_id:
                from store.models import StoreConfig

                StoreConfig.objects.filter(store_id=req.store_id).update(
                    design=req.design,
                    contact_email=req.contact_email,
                    brand_color=req.color_brand,
                    currency=req.currency,
                )
                if req.name and req.store.name != req.name:
                    req.store.name = req.name
                    req.store.save(update_fields=["name"])
        elif state == StorefrontRequest.PIPELINE_FAILED:
            req.status = StorefrontRequest.STATUS_FAILED
            req.deploy_status = StorefrontRequest.DEPLOY_FAILED
            req.completed_at = now
            if internal_error is not None:
                req.internal_error = internal_error
        else:
            req.status = StorefrontRequest.STATUS_SCAFFOLDING
            req.deploy_status = StorefrontRequest.DEPLOY_DEPLOYING
            req.completed_at = None
            if reset:
                req.internal_error = ""
                req.started_at = None
        req.event_version += 1
        req.save(update_fields=[
            "pipeline_state",
            "progress",
            "public_message",
            "last_heartbeat_at",
            "started_at",
            "completed_at",
            "internal_error",
            "status",
            "deploy_status",
            "event_version",
            "deployed_revision",
            "last_known_good_revision",
            "last_known_good_deployment_id",
        ])
        StorefrontDeploymentEvent.objects.create(
            storefront=req,
            version=req.event_version,
            state=state,
            progress=req.progress,
            message=message,
            snapshot=_safe_snapshot(req),
        )
        if state == StorefrontRequest.PIPELINE_LIVE and req.store_id:
            from store.domain_services import ensure_platform_domain
            ensure_platform_domain(req.store, active=True)
    return req


def heartbeat_deployment(request_id, *, expected_task_id=None):
    queryset = StorefrontRequest.objects.filter(pk=request_id)
    if expected_task_id is not None:
        queryset = queryset.filter(task_id=expected_task_id)
    queryset.update(last_heartbeat_at=timezone.now())


def ensure_initial_deployment_event(request_id):
    req = StorefrontRequest.objects.get(pk=request_id)
    if req.event_version:
        return req
    state = req.pipeline_state or StorefrontRequest.PIPELINE_ACCEPTED
    return record_deployment_state(
        request_id,
        state,
        message=f"deployment.{state}",
        reset=state == StorefrontRequest.PIPELINE_ACCEPTED,
    )
