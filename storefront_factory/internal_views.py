import uuid

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .jobs import (
    DeploymentAlreadyRunning,
    execute_storefront_deployment,
    reconcile_storefront_deployment,
)
from .models import StorefrontRequest


class StorefrontDeploymentExecuteView(APIView):
    """Capability-token endpoint called only by the private Vercel consumer."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, pk):
        raw_task_id = request.headers.get("X-Storefront-Task", "")
        try:
            task_id = uuid.UUID(raw_task_id)
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "Request unavailable."}, status=404)
        if not StorefrontRequest.objects.filter(pk=pk, task_id=task_id).exists():
            return Response({"detail": "Request unavailable."}, status=404)

        try:
            result = execute_storefront_deployment(pk, task_id=task_id)
        except (StorefrontRequest.DoesNotExist, DeploymentAlreadyRunning) as exc:
            if isinstance(exc, StorefrontRequest.DoesNotExist):
                return Response({"detail": "Request unavailable."}, status=404)
            return Response({"detail": "Processing is already in progress."}, status=409)

        # A failed delivery must remain non-2xx so Vercel Queue retries it.
        if result["status"] == "failed":
            return Response({"detail": "Processing could not be completed."}, status=503)
        return Response(result)


class StorefrontDeploymentReconcileView(APIView):
    """Provider reconciliation for the exact capability-scoped queue job."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, pk):
        try:
            task_id = uuid.UUID(request.headers.get("X-Storefront-Task", ""))
            attempt = max(1, int(request.headers.get("X-Reconciliation-Attempt", "1")))
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "Request unavailable."}, status=404)
        if not StorefrontRequest.objects.filter(pk=pk, task_id=task_id).exists():
            return Response({"detail": "Request unavailable."}, status=404)
        result = reconcile_storefront_deployment(pk, task_id=task_id, attempt=attempt)
        return Response(result)
