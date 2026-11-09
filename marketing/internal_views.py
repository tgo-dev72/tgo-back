import uuid

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .emails import send_campaign_batch
from .models import NewsletterCampaign


class CampaignDeliveryExecuteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, pk):
        try:
            task_id = uuid.UUID(request.headers.get("X-Campaign-Task", ""))
        except (TypeError, ValueError, AttributeError):
            return Response({"detail": "Campaign unavailable."}, status=404)
        if not NewsletterCampaign.objects.filter(pk=pk, delivery_task_id=task_id).exists():
            return Response({"detail": "Campaign unavailable."}, status=404)
        return Response(send_campaign_batch(pk, task_id=task_id))
