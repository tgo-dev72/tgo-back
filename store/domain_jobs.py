"""Durable dispatch and leased execution for domain verification."""
import os
import subprocess
import sys
import uuid
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import StorefrontDomain

QUEUE_TOPIC = "storefront-domains"


def _launch_local(domain_id, operation_id, action="verify"):
    subprocess.Popen(
        [sys.executable, str(settings.BASE_DIR / "manage.py"), "verify_storefront_domain",
         str(domain_id), str(operation_id), "--action", action],
        cwd=str(settings.BASE_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
    )


def enqueue_domain_verification(domain_id, *, oidc_token=""):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().get(pk=domain_id)
        lease_cutoff = timezone.now() - timedelta(minutes=10)
        if (domain.operation_id and domain.operation == "verify"
                and domain.operation_started_at and domain.operation_started_at >= lease_cutoff):
            return False
        operation_id = uuid.uuid4()
        domain.operation_id = operation_id
        domain.operation = "verify"
        domain.operation_started_at = timezone.now()
        domain.save(update_fields=["operation_id", "operation", "operation_started_at", "updated_at"])
    return _dispatch(domain_id, operation_id, "verify", oidc_token)


def enqueue_domain_disconnection(domain_id, *, oidc_token=""):
    from .domain_services import begin_disconnect
    begin_disconnect(domain_id)
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().get(pk=domain_id)
        operation_id = uuid.uuid4()
        domain.operation_id = operation_id
        domain.operation = "remove"
        domain.operation_started_at = timezone.now()
        domain.save(update_fields=["operation_id", "operation", "operation_started_at", "updated_at"])
    return _dispatch(domain_id, operation_id, "remove", oidc_token)


def _dispatch(domain_id, operation_id, action, oidc_token):
    if os.environ.get("VERCEL") == "1":
        if not oidc_token:
            _clear_failed_dispatch(domain_id, operation_id, "Verification queue capability is unavailable.")
            return False
        region = os.environ.get("VERCEL_REGION", "iad1")
        try:
            response = requests.post(
                f"https://{region}.vercel-queue.com/api/v3/topic/{QUEUE_TOPIC}",
                headers={"Authorization": f"Bearer {oidc_token}", "Content-Type": "application/json",
                         "Vqs-Idempotency-Key": str(operation_id), "Vqs-Retention-Seconds": "86400"},
                json={"action": action, "domainId": domain_id, "operationId": str(operation_id)}, timeout=15,
            )
        except requests.RequestException:
            _clear_failed_dispatch(domain_id, operation_id, "The domain operation could not be queued. Try again.")
            return False
        if response.status_code not in (201, 202):
            _clear_failed_dispatch(domain_id, operation_id, "Verification could not be queued. Try again.")
            return False
    else:
        try:
            _launch_local(domain_id, operation_id, action)
        except OSError:
            _clear_failed_dispatch(domain_id, operation_id, "The local domain worker could not start. Try again.")
            return False
    return True


def _clear_failed_dispatch(domain_id, operation_id, message):
    StorefrontDomain.objects.filter(pk=domain_id, operation_id=operation_id).update(
        operation_id=None, operation="", operation_started_at=None,
        status=StorefrontDomain.Status.FAILED, failure_code="queue_unavailable", public_message=message,
    )


def execute_domain_verification(domain_id, operation_id):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().filter(pk=domain_id).first()
        if not domain or str(domain.operation_id) != str(operation_id) or domain.operation != "verify":
            return False
    try:
        from .domain_services import verify_domain
        verify_domain(domain_id)
        return True
    except Exception:
        StorefrontDomain.objects.filter(pk=domain_id, operation_id=operation_id).update(
            status=StorefrontDomain.Status.FAILED,
            failure_code="verification_error",
            public_message="The connection check stopped unexpectedly. You can retry safely.",
        )
        raise
    finally:
        StorefrontDomain.objects.filter(pk=domain_id, operation_id=operation_id).update(
            operation_id=None, operation="", operation_started_at=None,
        )


def execute_domain_disconnection(domain_id, operation_id):
    with transaction.atomic():
        domain = StorefrontDomain.objects.select_for_update().filter(pk=domain_id).first()
        if not domain or str(domain.operation_id) != str(operation_id) or domain.operation != "remove":
            return False
    try:
        from .domain_services import finalize_disconnect
        finalize_disconnect(domain_id)
        return True
    except Exception:
        StorefrontDomain.objects.filter(pk=domain_id, operation_id=operation_id).update(
            status=StorefrontDomain.Status.FAILED, failure_code="disconnect_error",
            public_message="The hosting alias could not be removed. Retry disconnect safely.",
            operation_id=None, operation="", operation_started_at=None,
        )
        raise
