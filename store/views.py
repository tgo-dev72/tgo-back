import logging
from copy import deepcopy

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import transaction, IntegrityError
from django.db.models import Count, Max, Sum
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.utils import timezone

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from orders.models import Order
from accounts.models import MerchantProfile
from storefront_factory.models import StorefrontRequest
from storefront_factory.domains import default_storefront_url
from common.media_uploads import STOREFRONT_IMAGE_UPLOAD, image_folder, media_asset_url, signed_upload_payload

from .access import MANAGER_ROLES, OPERATIONS_ROLES, member_stores
from access_control.services import permitted_store, require_permission
from .models import (
    Store,
    StoreConfig,
    StoreCustomer,
    StorefrontAsset,
    StorefrontDomain,
    StorefrontPage,
    StoreMember,
    ensure_default_storefront_pages,
    sync_homepage_merchandising,
)
from .serializers import (
    CustomerSerializer,
    StoreConfigSerializer,
    StoreDeleteSerializer,
    StoreMemberCreateSerializer,
    StoreMemberRoleSerializer,
    StoreMemberSerializer,
    StoreLogoUploadSerializer,
    StorefrontAssetSerializer,
    StorefrontDomainSerializer,
    StorefrontPageSerializer,
    StorefrontPageCreateSerializer,
    StoreSerializer,
    STORE_SLUG_RE,
)


logger = logging.getLogger(__name__)


def _owned_store(user, store_id, permission="settings.update"):
    store = permitted_store(user, store_id, permission)
    if not StoreMember.objects.filter(user=user, store=store, role=StoreMember.Roles.OWNER).exists():
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("Only the store owner can manage domains.")
    return store


class StorefrontDomainListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_domain_connect"

    def get_throttles(self):
        # Reads are used to refresh settings and must never consume the
        # merchant's mutation allowance.
        return [] if self.request.method == "GET" else super().get_throttles()

    def get(self, request, store_id):
        store = _owned_store(request.user, store_id, "settings.view")
        from .domain_services import ensure_platform_domain
        ensure_platform_domain(store)
        return Response(StorefrontDomainSerializer(store.storefront_domains.all(), many=True).data)

    def post(self, request, store_id):
        from subscriptions.policies import enforce_operation
        from .domain_services import (
            DomainConnectionError, create_custom_domain, custom_domains_enabled,
            set_domain_redirect,
        )
        if not custom_domains_enabled():
            return Response({"code": "feature_disabled", "detail": "Custom domains are not enabled yet."}, status=503)
        store = _owned_store(request.user, store_id)
        enforce_operation(request.user, "domains.connect", context={"store": store})
        hostname = request.data.get("hostname", "")
        try:
            if request.data.get("include_www"):
                from .domain_validation import normalize_storefront_hostname
                normalized, _ = normalize_storefront_hostname(hostname)
                apex = normalized[4:] if normalized.startswith("www.") else normalized
                canonical, created = create_custom_domain(store, f"www.{apex}")
                redirect, _ = create_custom_domain(store, apex)
                set_domain_redirect(redirect, canonical.hostname)
                domains = [canonical, redirect]
            else:
                domain, created = create_custom_domain(store, hostname)
                domains = [domain]
        except DjangoValidationError as exc:
            return Response({"hostname": exc.messages}, status=400)
        except DomainConnectionError as exc:
            return Response({"code": exc.code, "detail": exc.public_message}, status=409)
        return Response({"domains": StorefrontDomainSerializer(domains, many=True).data}, status=201 if created else 200)


class StorefrontDomainDetailView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_domain_disconnect"

    def get_throttles(self):
        return [] if self.request.method == "GET" else super().get_throttles()

    def _domain(self, request, store_id, domain_id, permission="settings.view"):
        store = _owned_store(request.user, store_id, permission)
        return generics.get_object_or_404(StorefrontDomain, store=store, pk=domain_id)

    def get(self, request, store_id, domain_id):
        return Response(StorefrontDomainSerializer(self._domain(request, store_id, domain_id)).data)

    def delete(self, request, store_id, domain_id):
        from storefront_factory.vercel_deploy import DeployError
        from .domain_services import DomainConnectionError, disconnect_domain
        domain = self._domain(request, store_id, domain_id, "settings.update")
        try:
            disconnect_domain(domain.pk)
        except DomainConnectionError as exc:
            return Response({"code": exc.code, "detail": exc.public_message}, status=409)
        except DeployError:
            StorefrontDomain.objects.filter(pk=domain.pk).update(
                status=StorefrontDomain.Status.FAILED,
                failure_code="disconnect_error",
                public_message="The hosting alias could not be removed. Retry disconnect safely.",
                operation_id=None,
                operation="",
                operation_started_at=None,
            )
            return Response({"code": "disconnect_error", "detail": "The hosting alias could not be removed. Retry safely."}, status=409)
        return Response(status=204)


class StorefrontDomainVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_domain_verify"

    def post(self, request, store_id, domain_id):
        store = _owned_store(request.user, store_id)
        domain = generics.get_object_or_404(StorefrontDomain, store=store, pk=domain_id)
        from .domain_services import verify_domain
        # Domain verification is a bounded DNS/provider operation. Execute it
        # in the request so the merchant receives the real state instead of a
        # false 202 when the optional queue consumer is unavailable.
        StorefrontDomain.objects.filter(pk=domain.pk).update(
            operation_id=None, operation="", operation_started_at=None,
        )
        domain = verify_domain(domain.pk)
        logger.info(
            "Storefront domain verification completed store=%s domain=%s status=%s failure=%s provider_project=%s",
            store.pk, domain.pk, domain.status, domain.failure_code or "none",
            domain.provider_project_id or "none",
        )
        return Response(StorefrontDomainSerializer(domain).data)


class StorefrontDomainPrimaryView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "storefront_domain_primary"

    def post(self, request, store_id, domain_id):
        store = _owned_store(request.user, store_id)
        domain = generics.get_object_or_404(StorefrontDomain, store=store, pk=domain_id)
        from .domain_services import DomainConnectionError, make_primary
        try:
            domain = make_primary(domain.pk)
        except DomainConnectionError as exc:
            return Response({"code": exc.code, "detail": exc.public_message}, status=409)
        return Response(StorefrontDomainSerializer(domain).data)


class StoreAddressAvailabilityView(APIView):
    """Check a proposed public address without exposing another store."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "store_availability"

    def get(self, request):
        slug = request.query_params.get("slug", "").strip().lower()
        if not slug or len(slug) > 50 or not STORE_SLUG_RE.fullmatch(slug):
            return Response({"slug": slug, "available": False, "reason": "invalid"})
        available = not Store.objects.filter(slug=slug).exists()
        return Response({
            "slug": slug,
            "available": available,
            "reason": "available" if available else "taken",
        })


class StoreListCreateView(generics.ListCreateAPIView):
    """Merchant's stores.

    GET  → stores the current user is a member of.
    POST → creates a store, applies its storefront config (design, palette,
    currency, contact), and makes the caller its owner. Deployment starts only
    through the explicit scaffold endpoint, after callers have uploaded the
    logo and other creation assets."""
    serializer_class = StoreSerializer
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        if self.request.method == "POST":
            self.throttle_scope = "store_creation"
            self.throttle_classes = [ScopedRateThrottle]
        return super().get_throttles()

    def get_queryset(self):
        return member_stores(self.request.user).prefetch_related(
            "storefront_requests",
            "members__user",
        )

    def perform_create(self, serializer):
        from api.serializers import derive_palette
        from subscriptions.policies import enforce_operation

        with transaction.atomic():
            # Serialize store creation per merchant so simultaneous requests
            # cannot both pass the same account-level limit.
            get_user_model().objects.select_for_update().get(pk=self.request.user.pk)
            enforce_operation(self.request.user, "stores.create")
            config = serializer.validated_data.pop("config", None) or {}
            store = serializer.save()
            from catalog.models import RetailType
            from catalog.seeds import ensure_default_sections, ensure_retail_types
            if store.retail_type_id is None:
                general_retail_type = RetailType.objects.filter(key="general").first()
                if general_retail_type is None:
                    # Retail types are mandatory platform reference data. Heal
                    # a missing set here instead of turning store creation into
                    # an unhandled DoesNotExist response.
                    ensure_retail_types()
                    general_retail_type = RetailType.objects.get(key="general")
                store.retail_type = general_retail_type
                store.save(update_fields=["retail_type"])
            ensure_default_sections(store)
            StoreMember.objects.create(
                user=self.request.user,
                store=store,
                role=StoreMember.Roles.OWNER,
            )
            from subscriptions.services import sync_store_entitlement
            sync_store_entitlement(store)
            sc = StoreConfig.objects.get(store=store)
            desired_design = config.get("design", sc.design)
            from .design_access import can_use_design
            if not can_use_design(sc.plan, desired_design):
                from rest_framework import serializers as drf_serializers
                raise drf_serializers.ValidationError({
                    "config": {"design": "This design is not included in the current plan."}
                })
            sc.design = desired_design
            sc.contact_email = config.get("contact_email", sc.contact_email)
            sc.support_phone = config.get("support_phone", sc.support_phone)
            sc.whatsapp_phone = config.get("whatsapp_phone", sc.whatsapp_phone)
            sc.social_links = config.get("social_links", sc.social_links)
            sc.homepage_product_source = config.get("homepage_product_source", sc.homepage_product_source)
            sc.homepage_product_limit = config.get("homepage_product_limit", sc.homepage_product_limit)
            sc.brand_color = config.get("brand_color", sc.brand_color)
            sc.currency = config.get("currency", sc.currency)
            sc.save(update_fields=[
                "design", "contact_email", "support_phone", "whatsapp_phone", "social_links",
                "homepage_product_source", "homepage_product_limit", "brand_color", "currency",
            ])
            sync_homepage_merchandising(sc, publish=True)
            palette = derive_palette(config.get("brand_color", "#1F8A8C"))
            self._storefront_request = StorefrontRequest.objects.create(
                store=store,
                design=config.get("design", "basic"),
                name=store.name,
                slug=store.slug,
                contact_email=config.get("contact_email", "contact@example.com"),
                currency=config.get("currency", "MAD"),
                domain=default_storefront_url(store.slug),
                status=StorefrontRequest.STATUS_PENDING,
                deploy_status=StorefrontRequest.DEPLOY_NOT_DEPLOYED,
                **palette,
            )

    def create(self, request, *args, **kwargs):
        from subscriptions.policies import EntitlementDenied

        if not MerchantProfile.objects.filter(user=request.user).exists():
            return Response(
                {"detail": "Only merchant account owners can create a store."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            self.perform_create(serializer)
        except EntitlementDenied as exc:
            return Response(exc.as_dict(), status=status.HTTP_403_FORBIDDEN)
        except IntegrityError:
            return Response(
                {
                    "code": "store_address_taken",
                    "detail": "A store with this address already exists.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        headers = self.get_success_headers(serializer.data)
        return Response(
            self.get_serializer(serializer.instance).data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )


class StoreConfigRetrieveUpdateView(generics.RetrieveUpdateAPIView):
    """Merchant: read/update a store's per-store settings (StoreConfig)."""
    serializer_class = StoreConfigSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return member_stores(self.request.user, MANAGER_ROLES)

    def get_object(self):
        permission = "settings.view" if self.request.method == "GET" else "settings.update"
        store = permitted_store(self.request.user, self.kwargs["store_id"], permission)
        config, _ = StoreConfig.objects.get_or_create(store=store)
        return config

    def get_serializer_context(self):
        context = super().get_serializer_context()
        store = permitted_store(self.request.user, self.kwargs["store_id"], "settings.view")
        context["can_manage_payment_keys"] = StoreMember.objects.filter(
            user=self.request.user,
            store=store,
            role=StoreMember.Roles.OWNER,
        ).exists()
        context["store"] = store
        return context

    def update(self, request, *args, **kwargs):
        store = permitted_store(request.user, self.kwargs["store_id"], "settings.update")
        membership = StoreMember.objects.get(user=request.user, store=store)
        if membership.role != StoreMember.Roles.OWNER:
            if "payment_keys" in request.data or "payment_provider" in request.data:
                return Response(
                    {"detail": "Only the store owner can change payment settings."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            instance = self.get_object()
            data = request.data.copy()
            data.pop("payment_keys", None)
            data.pop("payment_provider", None)
            serializer = self.get_serializer(
                instance,
                data=data,
                partial=kwargs.pop("partial", False),
            )
            serializer.is_valid(raise_exception=True)
            self.perform_update(serializer)
            return Response(serializer.data)
        return super().update(request, *args, **kwargs)


class StoreEmailForwardingView(APIView):
    """Owner-managed, verified forwarding for the store's shared mail alias."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "store_email_routing"

    def get_throttles(self):
        return super().get_throttles() if self.request.method == "POST" else []

    def _store(self, request, store_id):
        return generics.get_object_or_404(
            member_stores(request.user, (StoreMember.Roles.OWNER,)), pk=store_id,
        )

    def _state(self, store, config):
        from .email_routing import alias_for, configured, destination_for, EmailRoutingError

        verified = False
        if config.email_forward_pending and configured():
            try:
                address = destination_for(config.email_forward_pending)
                verified = bool(address and address.get("verified"))
            except EmailRoutingError:
                pass
        return {
            "alias": alias_for(store),
            "configured": configured(),
            "active_destination": config.email_forward_active,
            "pending_destination": config.email_forward_pending,
            "pending_verified": verified,
            "status": "active" if config.email_forward_active and not config.email_forward_pending else
                      "pending" if config.email_forward_pending else "inactive",
            "sending_available": False,
        }

    def get(self, request, store_id):
        store = self._store(request, store_id)
        config, _ = StoreConfig.objects.get_or_create(store=store)
        return Response(self._state(store, config))

    def post(self, request, store_id):
        from .email_routing import (
            EmailRoutingError, activate, configured, disable, ensure_destination, destination_for,
        )

        store = self._store(request, store_id)
        from store_mail.models import StoreMailbox
        mailbox = StoreMailbox.objects.filter(store=store).first()
        if mailbox and mailbox.enabled:
            return Response(
                {"detail": "Disable the dashboard mailbox before changing legacy forwarding."},
                status=status.HTTP_409_CONFLICT,
            )
        config, _ = StoreConfig.objects.get_or_create(store=store)
        if not configured():
            return Response({"detail": "Store email forwarding is not configured yet."}, status=503)
        action = request.data.get("action", "set")
        try:
            if action == "disable":
                disable(store)
                config.email_forward_active = ""
                config.email_forward_pending = ""
                config.email_forward_rule_id = ""
            elif action == "activate":
                if not config.email_forward_pending:
                    return Response({"detail": "No destination is waiting for verification."}, status=400)
                address = destination_for(config.email_forward_pending)
                if not address or not address.get("verified"):
                    return Response({"detail": "Verify the destination inbox first, then try again."}, status=409)
                config.email_forward_rule_id = activate(store, config.email_forward_pending)
                config.email_forward_active = config.email_forward_pending
                config.email_forward_pending = ""
            elif action == "set":
                destination = str(request.data.get("destination", "")).strip().lower()
                try:
                    validate_email(destination)
                except DjangoValidationError:
                    return Response({"destination": "Enter a valid email address."}, status=400)
                if len(destination) > 90:
                    return Response({"destination": "Email address is too long."}, status=400)
                if destination == config.email_forward_active:
                    config.email_forward_pending = ""
                elif destination != config.email_forward_pending:
                    address = ensure_destination(destination)
                    if address.get("verified"):
                        config.email_forward_rule_id = activate(store, destination)
                        config.email_forward_active = destination
                        config.email_forward_pending = ""
                    else:
                        config.email_forward_pending = destination
            else:
                return Response({"action": "Unsupported action."}, status=400)
            config.save(update_fields=[
                "email_forward_active", "email_forward_pending", "email_forward_rule_id",
            ])
        except EmailRoutingError as exc:
            return Response({"detail": str(exc)}, status=502)
        return Response(self._state(store, config))


class StorefrontPageListCreateView(APIView):
    """List the store's complete page library or add an informational page."""

    permission_classes = [IsAuthenticated]

    def _store(self, request, store_id):
        permission = "storefront.view" if request.method == "GET" else "storefront.customize"
        return permitted_store(request.user, store_id, permission)

    def get(self, request, store_id):
        store = self._store(request, store_id)
        ensure_default_storefront_pages(store)
        pages = store.storefront_pages.order_by("sort_order", "page_key")
        return Response(StorefrontPageSerializer(pages, many=True).data)

    def post(self, request, store_id):
        store = self._store(request, store_id)
        ensure_default_storefront_pages(store)
        serializer = StorefrontPageCreateSerializer(
            data=request.data,
            context={"store": store},
        )
        serializer.is_valid(raise_exception=True)
        try:
            page = serializer.save()
        except IntegrityError:
            return Response(
                {"detail": "A page already uses this address."},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(StorefrontPageSerializer(page).data, status=status.HTTP_201_CREATED)


class StorefrontPageDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Read or save a merchant's private page draft."""

    serializer_class = StorefrontPageSerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "delete", "head", "options"]

    def get_object(self):
        permission = "storefront.view" if self.request.method == "GET" else "storefront.customize"
        store = permitted_store(self.request.user, self.kwargs["store_id"], permission)
        ensure_default_storefront_pages(store)
        return generics.get_object_or_404(
            store.storefront_pages,
            page_key=self.kwargs["page_key"],
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        page = self.get_object()
        if page.is_protected:
            return Response(
                {"detail": "This essential store page cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        page.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StorefrontPagePublishView(APIView):
    """Atomically promote the current draft to the public storefront."""

    permission_classes = [IsAuthenticated]

    def post(self, request, store_id, page_key):
        store = permitted_store(request.user, store_id, "storefront.publish")
        with transaction.atomic():
            ensure_default_storefront_pages(store)
            page = generics.get_object_or_404(
                StorefrontPage.objects.select_for_update(),
                store=store,
                page_key=page_key,
            )
            # Revalidate at the publication boundary, including drafts created
            # by older application versions.
            page.full_clean()
            page.published_sections = deepcopy(page.draft_sections)
            page.revision += 1
            page.published_at = timezone.now()
            page.updated_by = request.user
            page.save(update_fields=[
                "published_sections", "revision", "published_at", "updated_by", "updated_at",
            ])
            product_section = next(
                (section for section in page.published_sections if section.get("type") == "products"),
                None,
            )
            if product_section and page.page_key == StorefrontPage.PageKeys.HOME:
                settings_value = product_section.get("settings", {})
                StoreConfig.objects.filter(store=store).update(
                    homepage_product_source=settings_value.get("source", StoreConfig.HomepageProductSources.FEATURED),
                    homepage_product_limit=settings_value.get("limit", 8),
                )
        return Response(StorefrontPageSerializer(page).data)


class StorefrontAssetCreateView(generics.CreateAPIView):
    """Upload a validated visual for the authenticated merchant's storefront."""

    serializer_class = StorefrontAssetSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        store = permitted_store(self.request.user, self.kwargs["store_id"], "storefront.customize")
        serializer.context["store"] = store
        serializer.save(store=store, uploaded_by=self.request.user)


class StorefrontAssetSignView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, store_id):
        store = permitted_store(request.user, store_id, "storefront.customize")
        return Response(signed_upload_payload(
            image_folder(store.id, "storefront"),
            STOREFRONT_IMAGE_UPLOAD,
        ))


class StoreCustomerListView(generics.ListAPIView):
    """Merchant: the store's customers (ClientProfiles) + purchase summary."""
    serializer_class = CustomerSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        self.store = permitted_store(self.request.user, self.kwargs["store_id"], "customers.view")
        return (
            StoreCustomer.objects.filter(store=self.store)
            .select_related("profile__user")
            .order_by("-created_at")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        client_ids = [customer.profile_id for customer in queryset]
        stats = (
            Order.objects.filter(store=self.store, client_id__in=client_ids)
            .values("client_id")
            .annotate(orders=Count("id"), spent=Sum("amount"), last_order=Max("created_at"))
        )
        orders_by_user = {r["client_id"]: r["orders"] for r in stats}
        spent_by_user = {r["client_id"]: r["spent"] for r in stats}
        last_order_by_user = {r["client_id"]: r["last_order"] for r in stats}
        from marketing.models import Subscriber

        customer_emails = [customer.profile.user.email.lower() for customer in queryset]
        subscribed_emails = set(
            Subscriber.objects.filter(
                store=self.store,
                email__in=customer_emails,
                status=Subscriber.Status.ACTIVE,
            ).values_list("email", flat=True)
        )
        serializer = CustomerSerializer(
            queryset, many=True,
            context={
                "orders_by_user": orders_by_user,
                "spent_by_user": spent_by_user,
                "last_order_by_user": last_order_by_user,
                "subscribed_emails": {email.lower() for email in subscribed_emails},
            },
        )
        return Response(serializer.data)


def _get_store_for_user(user, store_id):
    return generics.get_object_or_404(
        Store.objects.filter(members__user=user),
        pk=store_id,
    )


def _require_admin_or_owner(user, store):
    """Return the StoreMember if user is owner/admin, else None."""
    try:
        sm = StoreMember.objects.get(user=user, store=store)
    except StoreMember.DoesNotExist:
        return None
    if sm.role not in (StoreMember.Roles.OWNER, StoreMember.Roles.ADMIN):
        return None
    return sm


def _preflight_store_deletion(store):
    """Build the complete deletion graph before mutating external systems."""
    from catalog.models import Product, VariantSelection

    product_ids = set(store.products.values_list("pk", flat=True))
    selection_ids = set(VariantSelection.objects.filter(
        variant__store=store,
    ).values_list("pk", flat=True))

    class StoreDeletionCollector(Collector):
        def related_objects(self, related_model, related_fields, objs):
            related = super().related_objects(related_model, related_fields, objs)
            if related_model is VariantSelection and selection_ids:
                return related.exclude(pk__in=selection_ids)
            if related_model is Product and product_ids:
                return related.exclude(pk__in=product_ids)
            return related

    collector = StoreDeletionCollector(using=store._state.db or "default")
    # Selector rows are explicitly removed before products in the real delete;
    # omit exactly those rows while checking the remaining deletion graph.
    collector.collect(store.products.all())
    collector.collect([store])
    return collector


class StoreDeleteView(APIView):
    """Permanently delete a store after two explicit typed confirmations.

    Safety gates: owner role, exact store slug and destructive phrase, no active
    fulfillment, and no deployment currently running.
    """

    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def delete(self, request, store_id):
        store = _get_store_for_user(request.user, store_id)
        # Serialize deletion against catalog/config mutations. PostgreSQL FK
        # checks also conflict with this row lock, preventing new tenant rows
        # from appearing between preflight and the final cascade.
        store = Store.objects.select_for_update().get(pk=store.pk)
        membership = StoreMember.objects.filter(user=request.user, store=store).first()
        if membership is None or membership.role != StoreMember.Roles.OWNER:
            return Response(
                {
                    "code": "store_delete_not_allowed",
                    "detail": "This operation could not be completed.",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = StoreDeleteSerializer(
            data=request.data,
            context={"request": request, "store": store},
        )
        if not serializer.is_valid():
            return Response(
                {
                    "code": "store_delete_confirmation_failed",
                    "detail": "The deletion could not be confirmed. Check the entered information and try again.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        active_statuses = [
            Order.Status.PENDING,
            Order.Status.CONFIRMED,
            Order.Status.SHIPPED,
        ]
        active_orders = store.orders.filter(status__in=active_statuses)
        if active_orders.exists():
            return Response(
                {
                    "code": "store_has_active_orders",
                    "detail": "This store cannot be deleted while orders still require attention.",
                    "active_orders": active_orders.count(),
                },
                status=status.HTTP_409_CONFLICT,
            )

        from storefront_factory.jobs import fail_stalled_storefront_deployment
        from storefront_factory.models import StorefrontRequest

        # Serialize deletion against migration preparation and update claims.
        # Keep these row locks through remote cleanup and the database cascade.
        deployments = store.storefront_requests.all()
        locked_deployments = list(deployments.select_for_update().order_by("pk"))
        from storefront_factory.repository_guards import migration_in_progress
        if any(migration_in_progress(item.pk) for item in locked_deployments):
            return Response(
                {"code": "storefront_migration_active", "detail": "Finish the repository migration before deleting this store."},
                status=status.HTTP_409_CONFLICT,
            )
        for deployment_id in deployments.filter(
            pipeline_state=StorefrontRequest.PIPELINE_ACCEPTED,
            attempt_count=0,
        ).values_list("id", flat=True):
            fail_stalled_storefront_deployment(deployment_id)

        active_pipeline_states = {
            StorefrontRequest.PIPELINE_ACCEPTED,
            StorefrontRequest.PIPELINE_PREPARING,
            StorefrontRequest.PIPELINE_PUBLISHING,
            StorefrontRequest.PIPELINE_PROVISIONING,
            StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
            StorefrontRequest.PIPELINE_BUILDING,
            StorefrontRequest.PIPELINE_VERIFYING,
        }
        deploying = deployments.filter(
            pipeline_state__in=active_pipeline_states,
        ).exclude(
            status=StorefrontRequest.STATUS_CREATED,
            deploy_status=StorefrontRequest.DEPLOY_DEPLOYED,
        ).exists()
        if deploying:
            return Response(
                {
                    "code": "store_deployment_active",
                    "detail": "This store is currently being prepared. Please try again shortly.",
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Prove the local cascade is collectable before removing any hosted,
        # DNS, repository, or email resource. This prevents a predictable
        # database PROTECT constraint from leaving a half-deleted storefront.
        try:
            _preflight_store_deletion(store)
        except (ProtectedError, RestrictedError):
            logger.warning("Store %s deletion graph contains protected records", store.pk)
            return Response(
                {
                    "code": "store_delete_blocked",
                    "detail": "The store contains protected data that must be resolved before deletion.",
                },
                status=status.HTTP_409_CONFLICT,
            )

        config = StoreConfig.objects.filter(store=store).first()
        from store_mail.models import StoreMailbox
        mailbox = StoreMailbox.objects.filter(store=store).first()
        if mailbox and mailbox.enabled:
            from store_mail.providers import disable_mailbox
            from .email_routing import EmailRoutingError
            try:
                disable_mailbox(store)
            except EmailRoutingError:
                logger.exception("Store %s dashboard mailbox cleanup failed", store.pk)
                return Response({
                    "code": "mailbox_cleanup_failed",
                    "detail": "The store mailbox could not be removed. Please try again later.",
                }, status=status.HTTP_502_BAD_GATEWAY)
        if config and (config.email_forward_active or config.email_forward_rule_id):
            from .email_routing import EmailRoutingError, disable
            try:
                disable(store)
            except EmailRoutingError:
                logger.exception("Store %s email forwarding cleanup failed", store.pk)
                return Response({
                    "code": "email_cleanup_failed",
                    "detail": "Store email forwarding could not be removed. Please try again later.",
                }, status=status.HTTP_502_BAD_GATEWAY)

        deployments = list(store.storefront_requests.all())
        try:
            from storefront_factory.services import (
                InfrastructureCleanupError,
                cleanup_storefront_infrastructure,
            )
            cleanup_storefront_infrastructure(deployments)
        except InfrastructureCleanupError as exc:
            logger.exception("Store %s infrastructure cleanup failed: %s", store.pk, exc)
            return Response(
                {
                    "code": "store_cleanup_failed",
                    "detail": "The store could not be deleted right now. Please try again later.",
                    "infrastructure_cleanup_failed": True,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        files = []
        for field_file in [
            store.logo,
            *(image.image for product in store.products.all() for image in product.images.all()),
            *(order.invoice for order in store.orders.all()),
            *(order.delivery_form for order in store.orders.all()),
        ]:
            if field_file and field_file.name:
                files.append((field_file.storage, field_file.name))
        for attachment in store.mail_conversations.values_list("messages__attachments__file", flat=True):
            if attachment:
                from django.core.files.storage import default_storage
                files.append((default_storage, attachment))

        with transaction.atomic():
            from subscriptions.models import SubscriptionPayment, UserSubscription
            from subscriptions.services import _release_reserved_credit

            # A store-scoped membership cannot remain active after its store
            # disappears. Keep the immutable payment ledger, but close live
            # access and invalidate unfinished checkouts before SET_NULL runs.
            UserSubscription.objects.filter(
                selected_store=store,
                status=UserSubscription.Status.ACTIVE,
            ).update(status=UserSubscription.Status.CANCELED, updated_at=timezone.now())
            pending_subscription_payments = SubscriptionPayment.objects.select_for_update().select_related("user").filter(
                selected_store=store,
                status=SubscriptionPayment.Status.PENDING,
            )
            for subscription_payment in pending_subscription_payments:
                _release_reserved_credit(subscription_payment)
                subscription_payment.status = SubscriptionPayment.Status.CANCELED
                subscription_payment.save(update_fields=["status", "updated_at"])
            # Product.section intentionally uses PROTECT so merchants cannot
            # accidentally remove a section that still contains products.
            # A confirmed store deletion is different: the whole tenant is
            # being removed, so delete its product tree first and then let the
            # normal Store cascades remove sections and the remaining data.
            from catalog.models import VariantSelection
            VariantSelection.objects.filter(variant__store=store).delete()
            store.products.all().delete()
            store.delete()

            def cleanup_files():
                for storage, name in files:
                    try:
                        storage.delete(name)
                    except Exception:
                        logger.exception("Could not delete store file %s", name)

            transaction.on_commit(cleanup_files)

        return Response(status=status.HTTP_204_NO_CONTENT)


class StoreMemberListCreateView(generics.ListCreateAPIView):
    """List members of a store, or add a new member.

    POST creates the AuthUser if needed (with a random unusable password)
    then creates the StoreMember link."""
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return StoreMemberCreateSerializer
        return StoreMemberSerializer

    def get_queryset(self):
        self.store = _get_store_for_user(self.request.user, self.kwargs["store_id"])
        require_permission(self.request.user, self.store, "staff.view")
        return StoreMember.objects.filter(store=self.store).select_related("user").order_by("-created_at")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        store = _get_store_for_user(request.user, self.kwargs["store_id"])
        me = require_permission(request.user, store, "staff.invite")

        ser = StoreMemberCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        email = ser.validated_data["email"].lower().strip()
        role = ser.validated_data["role"]
        if role == StoreMember.Roles.OWNER:
            return Response(
                {"detail": "Owner access cannot be assigned through team management."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from subscriptions.policies import EntitlementDenied, enforce_operation

        owner = store.members.select_related("user").get(role=StoreMember.Roles.OWNER).user
        # Team seats are account-scoped across every owned store. Serialize
        # the limit check and membership insert on that stable owner row.
        get_user_model().objects.select_for_update().only("pk").get(pk=owner.pk)
        try:
            enforce_operation(owner, "team.invite", context={"store": store})
        except EntitlementDenied as exc:
            return Response(exc.as_dict(), status=status.HTTP_403_FORBIDDEN)
        first_name = ser.validated_data.get("first_name", "")
        last_name = ser.validated_data.get("last_name", "")
        password = ser.validated_data.get("password", "")

        # Find or create user using the module-level auth model helper.
        User = get_user_model()
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"first_name": first_name, "last_name": last_name, "is_active": True},
        )
        if created:
            if password:
                user.set_password(password)
            else:
                user.set_unusable_password()
            user.save(update_fields=["password"])

        # Check not already a member
        if StoreMember.objects.filter(user=user, store=store).exists():
            return Response(
                {"detail": "This user is already a member of this store."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member = StoreMember.objects.create(user=user, store=store, role=role)
        return Response(
            StoreMemberSerializer(member).data,
            status=status.HTTP_201_CREATED,
        )


class StoreMemberDetailView(APIView):
    """Update a member's role (PATCH) or remove them from the store (DELETE)."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, store_id, member_id):
        store = _get_store_for_user(request.user, store_id)
        me = require_permission(request.user, store, "staff.update")

        member = generics.get_object_or_404(
            StoreMember.objects.filter(store=store),
            pk=member_id,
        )

        ser = StoreMemberRoleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_role = ser.validated_data["role"]

        if new_role == StoreMember.Roles.OWNER or member.role == StoreMember.Roles.OWNER:
            return Response(
                {"detail": "Store ownership cannot be changed through team role management."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Can't demote yourself
        if member.user == request.user and new_role != member.role:
            return Response(
                {"detail": "You cannot change your own role."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member.role = new_role
        member.save(update_fields=["role"])
        return Response(StoreMemberSerializer(member).data)

    def delete(self, request, store_id, member_id):
        store = _get_store_for_user(request.user, store_id)
        me = require_permission(request.user, store, "staff.revoke")

        member = generics.get_object_or_404(
            StoreMember.objects.filter(store=store),
            pk=member_id,
        )

        # Can't remove yourself
        if member.user == request.user:
            return Response(
                {"detail": "You cannot remove yourself from the store."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Can't remove the owner
        if member.role == StoreMember.Roles.OWNER:
            return Response(
                {"detail": "Cannot remove the store owner."},
                status=status.HTTP_403_FORBIDDEN,
            )

        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class StoreLogoView(APIView):
    """Upload or delete the store logo."""
    permission_classes = [IsAuthenticated]

    def post(self, request, store_id):
        store = generics.get_object_or_404(
            Store.objects.filter(members__user=request.user),
            pk=store_id,
        )
        me = _require_admin_or_owner(request.user, store)
        if me is None:
            return Response(
                {"detail": "Only owners or admins can change the store logo."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = StoreLogoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        logo = serializer.validated_data["logo"]

        previous_name = store.logo.name if store.logo else ""
        storage = store.logo.storage
        store.logo = logo
        store.save(update_fields=["logo"])
        if previous_name and previous_name != store.logo.name:
            storage.delete(previous_name)
        return Response({
            "logo_url": media_asset_url(store.logo, request=request) or None,
        })

    def delete(self, request, store_id):
        store = generics.get_object_or_404(
            Store.objects.filter(members__user=request.user),
            pk=store_id,
        )
        me = _require_admin_or_owner(request.user, store)
        if me is None:
            return Response(
                {"detail": "Only owners or admins can change the store logo."},
                status=status.HTTP_403_FORBIDDEN,
            )

        store.logo.delete(save=False)
        store.logo = None
        store.save(update_fields=["logo"])
        return Response(status=status.HTTP_204_NO_CONTENT)
