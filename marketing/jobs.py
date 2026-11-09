import os

import requests

from .models import NewsletterCampaign


QUEUE_TOPIC = "marketing-campaigns"


def production_queue_enabled():
    return os.environ.get("VERCEL", "").strip() == "1"


def enqueue_campaign_delivery(campaign_id, task_id, *, oidc_token):
    if not oidc_token:
        return False
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
    try:
        response = requests.post(
            f"https://{region}.vercel-queue.com/api/v3/topic/{QUEUE_TOPIC}",
            headers=headers,
            json={"campaignId": str(campaign_id), "taskId": str(task_id), "batch": 0},
            timeout=15,
        )
        if response.status_code in (201, 202):
            return True
    except requests.RequestException:
        pass
    NewsletterCampaign.objects.filter(pk=campaign_id, delivery_task_id=task_id).update(
        status=NewsletterCampaign.Status.FAILED,
    )
    return False
