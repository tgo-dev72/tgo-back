import secrets
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def backfill_domains(apps, schema_editor):
    Store = apps.get_model("store", "Store")
    StorefrontDomain = apps.get_model("store", "StorefrontDomain")
    StorefrontRequest = apps.get_model("storefront_factory", "StorefrontRequest")
    base = settings.STOREFRONT_BASE_DOMAIN.strip(".").lower()
    claimed = set()
    for store in Store.objects.all().iterator():
        request = StorefrontRequest.objects.filter(store_id=store.pk).order_by("-created_at").first()
        live = bool(request and request.pipeline_state == "live" and request.deploy_status == "deployed")
        hostname = f"{store.slug}.{base}"
        StorefrontDomain.objects.create(
            store_id=store.pk, hostname=hostname, display_hostname=hostname,
            kind="platform", status="active" if live else "pending", is_primary=live,
            ownership_token=secrets.token_urlsafe(32), provider_verified=live,
            certificate_status="valid" if live else "unknown",
            health_verified_at=timezone.now() if live else None,
        )
        claimed.add(hostname)
        try:
            raw = str(store.config.domain or "").strip()
            parsed = urlsplit(raw if "://" in raw else f"//{raw}")
            legacy = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
            labels = legacy.split(".")
            valid = (
                not parsed.username and not parsed.password and parsed.path in {"", "/"}
                and not parsed.query and not parsed.fragment and len(labels) >= 2
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                and not legacy.endswith((".local", ".localhost", ".internal", ".invalid", ".example", ".test", ".onion"))
                and legacy != base and not legacy.endswith(f".{base}")
            )
        except (Exception,):
            legacy, valid = "", False
        if valid and legacy != hostname and legacy not in claimed:
            StorefrontDomain.objects.create(
                store_id=store.pk, hostname=legacy, display_hostname=legacy,
                kind="custom", status="awaiting_ownership", is_primary=False,
                ownership_token=secrets.token_urlsafe(32),
                expected_dns_records=[{"type": "TXT", "name": f"_tijarago.{legacy}", "value": "Re-check this legacy domain in the dashboard"}],
                failure_code="legacy_unverified", public_message="Re-check ownership before using this domain.",
            )
            claimed.add(legacy)


class Migration(migrations.Migration):
    dependencies = [("store", "0023_storeconfig_default_locale"), ("storefront_factory", "0011_storefrontrepositorymigration")]
    operations = [
        migrations.CreateModel(
            name="StorefrontDomain",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("hostname", models.CharField(db_index=True, max_length=253, unique=True)),
                ("display_hostname", models.CharField(blank=True, default="", max_length=253)),
                ("kind", models.CharField(choices=[("platform", "Platform fallback"), ("custom", "Merchant custom domain"), ("provider", "Provider domain")], default="custom", max_length=16)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("awaiting_ownership", "Awaiting ownership proof"), ("awaiting_dns", "Awaiting routing DNS"), ("verifying", "Verifying"), ("active", "Active"), ("failed", "Needs attention"), ("removing", "Removing")], db_index=True, default="pending", max_length=24)),
                ("is_primary", models.BooleanField(default=False)),
                ("ownership_token", models.CharField(blank=True, default="", editable=False, max_length=96)),
                ("ownership_verified_at", models.DateTimeField(blank=True, null=True)),
                ("expected_dns_records", models.JSONField(blank=True, default=list)),
                ("provider_project_id", models.CharField(blank=True, default="", max_length=100)),
                ("provider_verified", models.BooleanField(default=False)),
                ("certificate_status", models.CharField(choices=[("unknown", "Unknown"), ("pending", "Pending"), ("valid", "Valid"), ("failed", "Failed")], default="unknown", max_length=16)),
                ("health_verified_at", models.DateTimeField(blank=True, null=True)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("next_retry_at", models.DateTimeField(blank=True, null=True)),
                ("failure_code", models.CharField(blank=True, default="", max_length=64)),
                ("public_message", models.CharField(blank=True, default="", max_length=240)),
                ("internal_diagnostics", models.TextField(blank=True, default="", editable=False)),
                ("operation_id", models.UUIDField(blank=True, editable=False, null=True, unique=True)),
                ("operation", models.CharField(blank=True, default="", editable=False, max_length=16)),
                ("operation_started_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("store", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="storefront_domains", to="store.store")),
            ],
            options={"ordering": ["-is_primary", "kind", "hostname"]},
        ),
        migrations.CreateModel(
            name="StorefrontDomainEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event", models.CharField(max_length=64)),
                ("public_message", models.CharField(blank=True, default="", max_length=240)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("domain", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="events", to="store.storefrontdomain")),
            ], options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(model_name="storefrontdomain", constraint=models.UniqueConstraint(condition=Q(is_primary=True), fields=("store",), name="uniq_primary_storefront_domain_per_store")),
        migrations.AddConstraint(model_name="storefrontdomain", constraint=models.CheckConstraint(condition=Q(is_primary=False) | Q(status="active"), name="primary_storefront_domain_must_be_active")),
        migrations.RunPython(backfill_domains, migrations.RunPython.noop),
    ]
