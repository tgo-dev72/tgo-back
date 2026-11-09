from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import NotFound

from api.designs import DesignNotFound, get_design
from .registry import eligible_designs, preview_payload


class ThemePreviewListView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response([preview_payload(design) for design in eligible_designs()])


class ThemePreviewDetailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, theme_key):
        try:
            design = get_design(theme_key)
        except DesignNotFound as exc:
            raise NotFound("Theme preview not found.") from exc
        payload = preview_payload(design, include_ticket=True)
        if not payload["preview_available"]:
            raise NotFound("Theme preview is not available.")
        return Response(payload)
