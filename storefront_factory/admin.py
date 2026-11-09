from django.contrib import admin, messages

from .deployment_state import ensure_initial_deployment_event, record_deployment_state
from .models import StorefrontCategory, StorefrontDeploymentEvent, StorefrontRequest
from .scaffold import ScaffoldError, scaffold_storefront
from .jobs import enqueue_storefront_deployment, vercel_oidc_token


class StorefrontCategoryInline(admin.TabularInline):
    model = StorefrontCategory
    extra = 1


class StorefrontDeploymentEventInline(admin.TabularInline):
    model = StorefrontDeploymentEvent
    extra = 0
    can_delete = False
    fields = ("version", "state", "progress", "message", "created_at")
    readonly_fields = fields
    ordering = ("-version",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StorefrontRequest)
class StorefrontRequestAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "design", "store", "pipeline_state", "progress", "attempt_count", "created_at")
    list_filter = ("pipeline_state", "status", "deploy_status", "design")
    inlines = [StorefrontCategoryInline, StorefrontDeploymentEventInline]
    actions = ["scaffold_selected", "queue_selected"]

    fieldsets = (
        (None, {"fields": ("name", "design", "slug", "domain", "contact_email", "currency")}),
        ("Color palette", {
            "fields": (
                "color_brand", "color_brand_dark", "color_brand_deep",
                "color_ink", "color_brand_light", "color_brand_light_1", "color_brand_light_2",
            ),
            "description": "Same token names as packages/storefront-core/src/styles/tokens.css — "
                            "these become the new app's src/index.css override block.",
        }),
        ("Dev server", {"fields": ("port",)}),
        ("Result (read-only)", {"fields": ("status", "app_path", "log")}),
        ("Vercel deploy (read-only)", {
            "fields": (
                "deploy_status", "github_commit_sha", "vercel_project_id",
                "source_repository", "deployment_repository", "deployment_branch",
                "deployment_app_path", "deployment_layout",
                "vercel_deployment_id", "cloudflare_dns_record_id",
                "operation", "target_revision", "deployed_revision",
                "last_known_good_revision", "last_known_good_deployment_id",
                "deployed_url", "deploy_log",
            ),
        }),
        ("Durable pipeline (read-only)", {
            "fields": (
                "pipeline_state", "progress", "public_message", "task_id",
                "attempt_count", "event_version", "started_at",
                "last_heartbeat_at", "completed_at", "internal_error",
            ),
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        readonly = [
            "status", "app_path", "log", "deploy_status", "vercel_project_id",
            "vercel_deployment_id", "github_commit_sha", "cloudflare_dns_record_id",
            "source_repository", "deployment_repository", "deployment_branch",
            "deployment_app_path", "deployment_layout",
            "operation", "target_revision", "deployed_revision",
            "last_known_good_revision", "last_known_good_deployment_id",
            "deployed_url", "deploy_log", "pipeline_state",
            "progress", "public_message", "task_id", "attempt_count", "event_version",
            "started_at", "last_heartbeat_at", "completed_at", "internal_error",
        ]
        # Once scaffolded, the on-disk app owns these values — editing them
        # here afterward wouldn't change anything on disk and would just be
        # confusing to read back.
        if obj and obj.status == StorefrontRequest.STATUS_CREATED:
            readonly += ["name", "design", "slug", "domain", "contact_email", "currency", "port"]
        return readonly

    @admin.action(description="Scaffold selected storefront(s) now")
    def scaffold_selected(self, request, queryset):
        for req in queryset:
            if req.status == StorefrontRequest.STATUS_CREATED:
                self.message_user(request, f"{req} was already scaffolded — skipping.", level=messages.WARNING)
                continue
            req.status = StorefrontRequest.STATUS_SCAFFOLDING
            req.save(update_fields=["status"])
            try:
                log_text = scaffold_storefront(req)
            except ScaffoldError as exc:
                req.status = StorefrontRequest.STATUS_FAILED
                req.log = str(exc)
                req.save(update_fields=["status", "log"])
                self.message_user(request, f"{req}: {exc}", level=messages.ERROR)
                continue
            except Exception as exc:  # noqa: BLE001 — surface anything unexpected rather than losing it
                req.status = StorefrontRequest.STATUS_FAILED
                req.log = f"Unexpected error: {exc}"
                req.save(update_fields=["status", "log"])
                self.message_user(request, f"{req}: unexpected error — {exc}", level=messages.ERROR)
                continue

            req.status = StorefrontRequest.STATUS_CREATED
            req.log = log_text
            req.save(update_fields=["status", "log", "app_path", "port"])
            self.message_user(request, f"{req}: scaffolded at apps/{req.slug} (port {req.port}).", level=messages.SUCCESS)

    @admin.action(description="Queue selected storefront deployment(s)")
    def queue_selected(self, request, queryset):
        from .repository_guards import migration_in_progress
        for req in queryset:
            if migration_in_progress(req.pk):
                self.message_user(request, f"{req}: repository migration is active.", level=messages.WARNING)
                continue
            if req.pipeline_state == StorefrontRequest.PIPELINE_LIVE:
                self.message_user(request, f"{req}: already live.", level=messages.WARNING)
                continue
            if req.pipeline_state == StorefrontRequest.PIPELINE_FAILED:
                StorefrontRequest.objects.filter(pk=req.pk).update(task_id=None)
                record_deployment_state(
                    req.pk,
                    StorefrontRequest.PIPELINE_ACCEPTED,
                    message="deployment.accepted",
                    reset=True,
                )
            else:
                ensure_initial_deployment_event(req.pk)
            queued = enqueue_storefront_deployment(
                req.pk,
                force=True,
                oidc_token=vercel_oidc_token(request),
            )
            level = messages.SUCCESS if queued else messages.WARNING
            message = "scheduled" if queued else "already scheduled or live"
            self.message_user(request, f"{req}: {message}.", level=level)
