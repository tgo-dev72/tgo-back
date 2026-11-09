import json

from django.http import JsonResponse
from django.views import View
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from .domain_jobs import execute_domain_disconnection, execute_domain_verification


@method_decorator(csrf_exempt, name="dispatch")
class DomainQueueView(View):
    def post(self, request):
        # Vercel Queue invokes this function through its OIDC-authenticated
        # delivery path; reject ordinary public calls before reading the body.
        try:
            body = json.loads(request.body or b"{}")
            if request.headers.get("X-Domain-Operation", "") != str(body.get("operationId", "")):
                return JsonResponse({"detail": "Request unavailable."}, status=404)
            executor = execute_domain_disconnection if body.get("action") == "remove" else execute_domain_verification
            accepted = executor(body["domainId"], body["operationId"])
        except (KeyError, ValueError, TypeError):
            return JsonResponse({"detail": "Invalid queue payload."}, status=400)
        return JsonResponse({"accepted": accepted})
