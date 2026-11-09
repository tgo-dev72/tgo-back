from django.http import JsonResponse

from accounts.cookies import resolve_tenant
from .models import ThemeDemoStore
from .registry import signup_url


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class DemoStoreMutationGuardMiddleware:
    """Fail closed for every tenant-scoped write aimed at an official demo."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method.upper() not in SAFE_METHODS:
            slug = resolve_tenant(request)
            profile = None
            if slug:
                profile = ThemeDemoStore.objects.filter(
                    store__slug=slug,
                ).values("theme_key").first()
            if profile:
                response = JsonResponse({
                    "code": "theme_demo_read_only",
                    "detail": "This is a demonstration store. Create your own TijaraGO store to start selling.",
                    "signup_url": signup_url(profile["theme_key"]),
                }, status=403)
                response["Cache-Control"] = "no-store"
                return response
        return self.get_response(request)
