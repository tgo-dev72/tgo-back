"""Staff-only monitoring and configuration API for the platform control room."""

import asyncio
import re
import time
from datetime import timedelta
from decimal import Decimal
from time import monotonic

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.handlers.asgi import ASGIRequest
from django.db import connection, transaction
from django.http import StreamingHttpResponse
from django.db.models import Count, DecimalField, F, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from asgiref.sync import sync_to_async
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from api.views import EventStreamRenderer, _deployment_event_chunk, _deployment_events_after, _reconcile_deployment_truth

from accounts.authentication import StaffCookieJWTAuthentication
from accounts.models import MerchantProfile
from catalog.models import Product, StockVariant
from marketing.models import NewsletterCampaign, Subscriber
from orders.models import Order, QuantityException
from payments.models import PaymentTransaction, PlatformPaymentGateway
from payments.gateways import get_platform_gateway
from payments.gateways.availability import platform_gateway_descriptors
from store.models import Store, StoreMember
from storefront_factory.models import StorefrontRequest
from storefront_factory.deployment_state import record_deployment_state
from storefront_factory.jobs import enqueue_storefront_deployment, vercel_oidc_token
from storefront_factory.release_status import approved_release_revision, storefront_release_status
from subscriptions.models import EntitlementDefinition, PlanEntitlement, SubscriptionPayment, SubscriptionPlan, UserSubscription
from subscriptions.entitlements import configure_plan_entitlements
from subscriptions.policies import business_rule_catalog
from .models import PlatformMonitoringSettings, PlatformPresentation, UserAdministrationEvent
from .monitoring import collect_operational_incidents, dispatch_monitoring_alert, send_monitoring_test
from .serializers import AdminUserUpdateSerializer, EntitlementDefinitionSerializer, PlatformMonitoringSettingsSerializer, PlatformPresentationSerializer, StaffCreateSerializer, StaffUpdateSerializer, SubscriptionPlanSerializer
from .staff_roles import (
    PERMISSION_CATALOG,
    assign_staff_role,
    permission_catalog_payload,
    role_catalog_payload,
    role_slug_for_user,
)

User = get_user_model()
ZERO = Decimal("0.00")


def _decimal(value):
    return float(value or ZERO)


def _iso(value):
    return value.isoformat() if value else None


def _percentage(part, whole):
    return round((part / whole) * 100, 1) if whole else 0.0


def _subscription_provider_status():
    try:
        gateway = get_platform_gateway()
        descriptor = gateway.public_descriptor(gateway.platform_credentials())
    except Exception:
        return {
            "code": settings.PLATFORM_PAYMENT_GATEWAY,
            "name": "Payment provider",
            "configured": False,
            "sandbox": False,
            "checkout_credentials_configured": False,
            "transaction_verification_configured": False,
            "transaction_verification_required": False,
        }
    details = descriptor.pop("details", {})
    descriptor.pop("credential_fields", None)
    return {
        **descriptor,
        **details,
    }


class AdminPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class StaffAPIView(APIView):
    """The platform dashboard never accepts a merchant-owner cookie."""

    authentication_classes = [StaffCookieJWTAuthentication]
    permission_classes = [IsAdminUser]

    @staticmethod
    def require_permission(request, permission, message="You do not have access to this platform area."):
        if not request.user.has_perm(permission):
            raise PermissionDenied(message)


class PublicPlatformPresentationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(PlatformPresentationSerializer(PlatformPresentation.load()).data)


class PlatformPresentationView(StaffAPIView):
    def get(self, request):
        return Response(PlatformPresentationSerializer(PlatformPresentation.load()).data)

    def patch(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a platform owner can update the showroom profile.")
        profile = PlatformPresentation.load()
        serializer = PlatformPresentationSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)


class PlatformMonitoringSettingsView(StaffAPIView):
    def get(self, request):
        record = PlatformMonitoringSettings.load()
        payload = PlatformMonitoringSettingsSerializer(record).data
        payload["incidents"] = collect_operational_incidents()
        return Response(payload)

    def patch(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a platform owner can update monitoring settings.")
        record = PlatformMonitoringSettings.load()
        serializer = PlatformMonitoringSettingsSerializer(record, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        payload = serializer.data
        payload["incidents"] = collect_operational_incidents()
        return Response(payload)


class PlatformMonitoringTestView(StaffAPIView):
    def post(self, request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a platform owner can send monitoring tests.")
        record = PlatformMonitoringSettings.load()
        if not record.pk:
            record.updated_by = request.user
            record.save()
        if not send_monitoring_test(record):
            return Response({"detail": "The monitoring email could not be delivered."}, status=502)
        return Response(PlatformMonitoringSettingsSerializer(record).data)


class PlatformMonitoringCronView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not settings.CRON_SECRET:
            return Response({"detail": "Platform monitoring is not configured."}, status=503)
        import hmac
        if not hmac.compare_digest(supplied, settings.CRON_SECRET):
            return Response({"detail": "Unauthorized."}, status=401)
        result = dispatch_monitoring_alert()
        return Response(result, status=200 if result.get("delivered") is not False or result["status"] in {"healthy", "unchanged", "disabled"} else 502)


class AdminSessionView(StaffAPIView):
    def get(self, request):
        user = request.user
        return Response({
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_superuser": user.is_superuser,
                "permissions": {
                    "manage_pricing": user.has_perm("subscriptions.change_subscriptionplan"),
                    "manage_users": user.has_perm("accounts.change_authuser"),
                    "manage_staff": user.is_superuser,
                },
                "access": {
                    "overview": True,
                    "stores": user.has_perm("store.view_store"),
                    "users": user.has_perm("accounts.view_authuser"),
                    "staff": user.is_superuser,
                    "orders": user.has_perm("orders.view_order"),
                    "payments": user.has_perm("payments.view_paymenttransaction"),
                    "membership_payments": user.has_perm("subscriptions.view_subscriptionpayment"),
                    "billing": (
                        user.has_perm("payments.view_paymenttransaction")
                        or user.has_perm("subscriptions.view_subscriptionpayment")
                    ),
                    "pricing": user.has_perm("subscriptions.view_subscriptionplan"),
                    "deployments": user.has_perm("storefront_factory.view_storefrontrequest"),
                    "presentation": True,
                    "monitoring": True,
                    "system": True,
                },
                "last_login": _iso(user.last_login),
            }
        })


def _staff_identity_warnings(user):
    warnings = []
    if MerchantProfile.objects.filter(user=user).exists():
        warnings.append("merchant_account")
    if user.store_memberships.exists():
        warnings.append("store_team")
    if hasattr(user, "client_profile"):
        warnings.append("store_customer")
    return warnings


def _staff_payload(user):
    known_permissions = [
        code for code in PERMISSION_CATALOG
        if user.is_superuser or user.has_perm(code)
    ]
    return {
        "id": user.pk,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
        "role": role_slug_for_user(user),
        "permissions": known_permissions,
        "identity_warnings": _staff_identity_warnings(user),
        "last_login": _iso(user.last_login),
        "date_joined": _iso(user.date_joined),
    }


class StaffManagementView(StaffAPIView):
    """Owner-only staff directory and curated authorization catalog."""

    @staticmethod
    def _require_owner(request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a platform owner can manage staff accounts and roles.")

    def get(self, request):
        self._require_owner(request)
        staff = User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)).prefetch_related("groups").order_by(
            "-is_superuser", "email",
        )
        return Response({
            "staff": [_staff_payload(user) for user in staff],
            "roles": role_catalog_payload(),
            "permissions": permission_catalog_payload(),
            "authorization_layers": [
                {"key": "identity", "name": "Dedicated staff identity", "description": "New staff accounts cannot reuse merchant, store-team or customer identities."},
                {"key": "session", "name": "Separate staff session", "description": "The platform dashboard accepts only the staff-scoped secure cookie."},
                {"key": "roles", "name": "Role permissions", "description": "Curated Django groups grant only the permissions required for each responsibility."},
                {"key": "owner", "name": "Owner-only staff control", "description": "Only a platform superuser can create staff accounts or change their role and access."},
                {"key": "suspension", "name": "Immediate suspension", "description": "Deactivating an account invalidates authenticated API access without deleting its audit history."},
            ],
        })

    def post(self, request):
        self._require_owner(request)
        serializer = StaffCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            user = User.objects.create_user(
                email=data["email"],
                password=data["password"],
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
                is_staff=True,
                is_active=True,
            )
            assign_staff_role(user, data["role"])
        return Response(_staff_payload(user), status=201)


class StaffMemberDetailView(StaffAPIView):
    def patch(self, request, staff_id):
        StaffManagementView._require_owner(request)
        user = User.objects.filter(pk=staff_id, is_staff=True).first()
        if not user:
            return Response({"detail": "Staff account not found."}, status=404)
        if user.is_superuser:
            return Response(
                {"detail": "Platform-owner accounts must be managed through the secure server administration process."},
                status=409,
            )
        serializer = StaffUpdateSerializer(data=request.data, partial=True, context={"user": user})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        with transaction.atomic():
            changed_fields = []
            for field in ("first_name", "last_name", "is_active"):
                if field in data:
                    setattr(user, field, data[field])
                    changed_fields.append(field)
            if "password" in data:
                user.set_password(data["password"])
                changed_fields.append("password")
            user.is_staff = True
            changed_fields.append("is_staff")
            user.save(update_fields=sorted(set(changed_fields)))
            if "role" in data:
                assign_staff_role(user, data["role"])
        return Response(_staff_payload(user))


class PlanListCreateView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "subscriptions.view_subscriptionplan")
        configured_keys = EntitlementDefinition.objects.values_list("key", flat=True)
        return Response({
            "plans": SubscriptionPlanSerializer(SubscriptionPlan.objects.all(), many=True).data,
            "capabilities": EntitlementDefinitionSerializer(EntitlementDefinition.objects.all(), many=True).data,
            "rule_templates": business_rule_catalog(configured_keys),
            "provider": _subscription_provider_status(),
        })

    def post(self, request):
        if not request.user.has_perm("subscriptions.add_subscriptionplan"):
            raise PermissionDenied("Pricing creation requires the subscription-plan permission.")
        serializer = SubscriptionPlanSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data, status=201)


class PlanDetailView(StaffAPIView):
    def patch(self, request, plan_id):
        if not request.user.has_perm("subscriptions.change_subscriptionplan"):
            raise PermissionDenied("Pricing changes require the subscription-plan permission.")
        with transaction.atomic():
            # Serialize plan edits so two staff members cannot overwrite the
            # same revision or produce mismatched plan/entitlement snapshots.
            plan = SubscriptionPlan.objects.select_for_update().filter(pk=plan_id).first()
            if not plan:
                return Response({"detail": "Plan not found."}, status=404)
            serializer = SubscriptionPlanSerializer(
                plan,
                data=request.data,
                partial=True,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, plan_id):
        if not request.user.has_perm("subscriptions.delete_subscriptionplan"):
            raise PermissionDenied("Pricing deletion requires the subscription-plan permission.")
        plan = SubscriptionPlan.objects.filter(pk=plan_id).first()
        if not plan:
            return Response(status=204)
        if plan.is_free:
            return Response({"detail": "The Free plan cannot be deleted."}, status=409)
        if UserSubscription.objects.filter(plan=plan, status=UserSubscription.Status.ACTIVE).exists():
            return Response({"detail": "Deactivate this plan; active subscribers still use it."}, status=409)
        if UserSubscription.objects.filter(plan=plan).exists() or SubscriptionPayment.objects.filter(plan=plan).exists():
            return Response({"detail": "This plan has billing history. Archive it instead of deleting it."}, status=409)
        plan.delete()
        return Response(status=204)


class CapabilityListCreateView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "subscriptions.view_entitlementdefinition")
        configured_keys = EntitlementDefinition.objects.values_list("key", flat=True)
        return Response({
            "capabilities": EntitlementDefinitionSerializer(
                EntitlementDefinition.objects.all(), many=True,
            ).data,
            "rule_templates": business_rule_catalog(configured_keys),
        })

    def post(self, request):
        if not request.user.has_perm("subscriptions.add_entitlementdefinition"):
            raise PermissionDenied("Capability creation requires the entitlement-definition permission.")
        serializer = EntitlementDefinitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            capability = serializer.save(updated_by=request.user)
            if capability.is_active:
                for plan in SubscriptionPlan.objects.select_for_update().all():
                    configure_plan_entitlements(
                        plan, {capability.key: capability.default_value}, actor=request.user,
                    )
        return Response(serializer.data, status=201)


class CapabilityDetailView(StaffAPIView):
    def patch(self, request, capability_id):
        if not request.user.has_perm("subscriptions.change_entitlementdefinition"):
            raise PermissionDenied("Capability changes require the entitlement-definition permission.")
        capability = EntitlementDefinition.objects.filter(pk=capability_id).first()
        if not capability:
            return Response({"detail": "Capability not found."}, status=404)
        was_active = capability.is_active
        serializer = EntitlementDefinitionSerializer(capability, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            capability = serializer.save(updated_by=request.user)
            if capability.is_active != was_active:
                for plan in SubscriptionPlan.objects.select_for_update().all():
                    configure_plan_entitlements(plan, {}, actor=request.user)
        return Response(serializer.data)

    def delete(self, request, capability_id):
        if not request.user.has_perm("subscriptions.delete_entitlementdefinition"):
            raise PermissionDenied("Capability deletion requires the entitlement-definition permission.")
        capability = EntitlementDefinition.objects.filter(pk=capability_id).first()
        if not capability:
            return Response(status=204)
        if PlanEntitlement.objects.filter(definition=capability).exists():
            return Response({
                "detail": "This capability is used by one or more plans. Archive it instead of deleting it.",
            }, status=409)
        capability.delete()
        return Response(status=204)


class OverviewView(StaffAPIView):
    """Platform KPIs, trends, workload and actionable alerts in one request."""

    def get(self, request):
        now = timezone.now()
        last_30_days = now - timedelta(days=30)
        trend_start = (now - timedelta(days=13)).date()

        non_cancelled = Order.objects.exclude(status=Order.Status.CANCELLED)
        realized = Order.objects.filter(
            Q(is_paid=Order.IsPaid.CONFIRMED)
            | Q(payment_mode=Order.PaymentMode.COD, status=Order.Status.DELIVERED)
        )

        order_status = {
            row["status"]: row["count"]
            for row in Order.objects.values("status").annotate(count=Count("id"))
        }
        payment_status = {
            row["status"]: row["count"]
            for row in PaymentTransaction.objects.values("status").annotate(count=Count("id"))
        }
        deployment_status = {
            row["deploy_status"]: row["count"]
            for row in StorefrontRequest.objects.values("deploy_status").annotate(count=Count("id"))
        }

        trend_rows = {
            row["day"]: row
            for row in non_cancelled.filter(created_at__date__gte=trend_start)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(orders=Count("id"), volume=Coalesce(Sum("amount"), ZERO))
            .order_by("day")
        }
        trend = []
        for offset in range(14):
            day = trend_start + timedelta(days=offset)
            row = trend_rows.get(day)
            trend.append({
                "date": day.isoformat(),
                "orders": row["orders"] if row else 0,
                "volume": _decimal(row["volume"]) if row else 0.0,
            })

        retail_mix = list(
            Store.objects.values(
                key=F("retail_type__key"), label=F("retail_type__label")
            )
            .annotate(count=Count("id"))
            .order_by("-count", "label")
        )
        for item in retail_mix:
            item["key"] = item["key"] or "unconfigured"
            item["label"] = item["label"] or "Unconfigured"

        terminal_payments = payment_status.get(PaymentTransaction.Status.CONFIRMED, 0) + payment_status.get(
            PaymentTransaction.Status.FAILED, 0
        )
        confirmed_payments = payment_status.get(PaymentTransaction.Status.CONFIRMED, 0)

        recent_orders = Order.objects.select_related("store").all()[:6]
        recent_stores = Store.objects.select_related("retail_type", "config").prefetch_related("members__user")[:6]
        recent_deployments = StorefrontRequest.objects.select_related("store").all()[:6]

        return Response({
            "generated_at": now.isoformat(),
            "metrics": {
                "users": User.objects.count(),
                "new_users_30d": User.objects.filter(date_joined__gte=last_30_days).count(),
                "stores": Store.objects.count(),
                "new_stores_30d": Store.objects.filter(created_at__gte=last_30_days).count(),
                "products": Product.objects.count(),
                "active_products": Product.objects.filter(is_active=True).count(),
                "orders": Order.objects.count(),
                "orders_30d": Order.objects.filter(created_at__gte=last_30_days).count(),
                "gross_volume": _decimal(non_cancelled.aggregate(total=Sum("amount"))["total"]),
                "realized_revenue": _decimal(realized.aggregate(total=Sum("amount"))["total"]),
                "payment_success_rate": _percentage(confirmed_payments, terminal_payments),
                "deployed_storefronts": deployment_status.get(StorefrontRequest.DEPLOY_DEPLOYED, 0),
            },
            "order_status": order_status,
            "payment_status": payment_status,
            "deployment_status": deployment_status,
            "trend": trend,
            "retail_mix": retail_mix,
            "alerts": self._alerts(now),
            "recent": {
                "orders": [_serialize_order(order) for order in recent_orders],
                "stores": [_serialize_store(store) for store in recent_stores],
                "deployments": [_serialize_deployment(item) for item in recent_deployments],
            },
        })

    @staticmethod
    def _alerts(now):
        definitions = [
            (
                "failed_deployments", "critical", "Failed deployments",
                StorefrontRequest.objects.filter(deploy_status=StorefrontRequest.DEPLOY_FAILED).count(),
                "Storefronts need a deployment retry or provider review.", "/deployments?status=failed",
            ),
            (
                "failed_payments", "critical", "Payment failures in the last 24 hours",
                PaymentTransaction.objects.filter(
                    status=PaymentTransaction.Status.FAILED,
                    updated_at__gte=now - timedelta(hours=24),
                ).count(),
                "Review provider responses and affected orders.", "/payments?status=failed",
            ),
            (
                "stale_orders", "warning", "Orders pending over 24 hours",
                Order.objects.filter(
                    status=Order.Status.PENDING,
                    created_at__lt=now - timedelta(hours=24),
                ).count(),
                "These orders may be awaiting payment or merchant action.", "/orders?status=pending",
            ),
            (
                "stockouts", "warning", "Active variants out of stock",
                StockVariant.objects.filter(product__is_active=True, quantity__lte=F("reserved")).count(),
                "Stock is fully reserved or depleted.", "/system",
            ),
            (
                "unstocked_products", "info", "Active products without inventory",
                Product.objects.filter(is_active=True, variants__isnull=True).count(),
                "Checkout requires at least one stock variant.", "/system",
            ),
        ]
        return [
            {"key": key, "severity": severity, "title": title, "count": count, "detail": detail, "href": href}
            for key, severity, title, count, detail, href in definitions
            if count
        ]


class StoreListView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "store.view_store")
        order_volume = (
            Order.objects.filter(store_id=OuterRef("pk"))
            .exclude(status=Order.Status.CANCELLED)
            .values("store_id")
            .annotate(total=Sum("amount"))
            .values("total")
        )
        queryset = (
            Store.objects.select_related("retail_type", "config")
            .prefetch_related(
                "members__user",
                Prefetch(
                    "storefront_requests",
                    queryset=StorefrontRequest.objects.select_related("store").order_by("-created_at"),
                    to_attr="admin_deployments",
                ),
            )
            .annotate(
                admin_members_count=Count("members", distinct=True),
                admin_products_count=Count("products", distinct=True),
                admin_orders_count=Count("orders", distinct=True),
                admin_customers_count=Count("store_customers", distinct=True),
                admin_gross_volume=Coalesce(
                    Subquery(order_volume, output_field=DecimalField(max_digits=12, decimal_places=2)),
                    Value(ZERO),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
            )
        )
        search = request.query_params.get("search", "").strip()
        retail_type = request.query_params.get("retail_type", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(slug__icontains=search)
                | Q(members__user__email__icontains=search)
            ).distinct()
        if retail_type:
            queryset = queryset.filter(retail_type__key=retail_type)
        queryset = queryset.order_by("-created_at", "-id")

        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        results = []
        for store in page:
            payload = _serialize_store(store)
            payload.update({
                "members_count": store.admin_members_count,
                "products_count": store.admin_products_count,
                "orders_count": store.admin_orders_count,
                "customers_count": store.admin_customers_count,
                "gross_volume": _decimal(store.admin_gross_volume),
            })
            latest = store.admin_deployments[0] if store.admin_deployments else None
            payload["deployment"] = _serialize_deployment(latest) if latest else None
            results.append(payload)
        return paginator.get_paginated_response(results)


class UserListView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "accounts.view_authuser")
        queryset = User.objects.annotate(
            stores_count=Count("store_memberships__store", distinct=True),
            customer_stores_count=Count("client_profile__store_customers__store", distinct=True),
        ).order_by("-date_joined")
        search = request.query_params.get("search", "").strip()
        role = request.query_params.get("role", "").strip()
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        if role == "staff":
            queryset = queryset.filter(is_staff=True)
        elif role == "merchant":
            queryset = queryset.filter(
                Q(merchant_profile__isnull=False) | Q(store_memberships__isnull=False)
            ).distinct()
        elif role == "customer":
            queryset = queryset.filter(client_profile__isnull=False)

        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        results = []
        for user in page:
            store_roles = sorted(set(user.store_memberships.values_list("role", flat=True)))
            principal_types = []
            if user.is_superuser:
                principal_types.append("platform_superadmin")
            elif user.is_staff:
                principal_types.append("platform_staff")
            if hasattr(user, "merchant_profile"):
                principal_types.append("merchant_account")
            if StoreMember.Roles.OWNER in store_roles:
                principal_types.append("merchant_owner")
            if any(role != StoreMember.Roles.OWNER for role in store_roles):
                principal_types.append("merchant_team")
            if hasattr(user, "client_profile"):
                principal_types.append("store_customer")
            if not principal_types:
                principal_types.append("unclassified")
            results.append(_user_payload(user, request.user, principal_types, store_roles))
        return paginator.get_paginated_response(results)


def _user_delete_blockers(user):
    blockers = []
    if user.is_superuser:
        blockers.append("platform_superuser")
    if user.store_memberships.filter(role=StoreMember.Roles.OWNER).exists():
        blockers.append("store_owner")
    if user.subscription_payments.exists():
        blockers.append("billing_history")
    return blockers


def _user_payload(user, actor, principal_types=None, store_roles=None):
    store_roles = store_roles if store_roles is not None else sorted(set(user.store_memberships.values_list("role", flat=True)))
    if principal_types is None:
        principal_types = []
        if user.is_superuser:
            principal_types.append("platform_superadmin")
        elif user.is_staff:
            principal_types.append("platform_staff")
        if hasattr(user, "merchant_profile"):
            principal_types.append("merchant_account")
        if StoreMember.Roles.OWNER in store_roles:
            principal_types.append("merchant_owner")
        if any(role != StoreMember.Roles.OWNER for role in store_roles):
            principal_types.append("merchant_team")
        if hasattr(user, "client_profile"):
            principal_types.append("store_customer")
        if not principal_types:
            principal_types.append("unclassified")
    blockers = _user_delete_blockers(user)
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "name": (f"{user.first_name} {user.last_name}").strip() or user.email,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "email_verified": user.email_verified_at is not None,
        "principal_types": principal_types,
        "store_roles": store_roles,
        "stores_count": getattr(user, "stores_count", user.store_memberships.values("store_id").distinct().count()),
        "customer_stores_count": getattr(user, "customer_stores_count", user.client_profile.store_customers.values("store_id").distinct().count() if hasattr(user, "client_profile") else 0),
        "date_joined": _iso(user.date_joined),
        "last_login": _iso(user.last_login),
        "is_self": user.pk == actor.pk,
        "can_delete": user.pk != actor.pk and not blockers,
        "delete_blockers": blockers,
    }


class UserDetailView(StaffAPIView):
    @staticmethod
    def _require_owner(request):
        if not request.user.is_superuser:
            raise PermissionDenied("Only a platform owner can modify user accounts.")

    def get_object(self, user_id):
        return User.objects.filter(pk=user_id).first()

    def get(self, request, user_id):
        self.require_permission(request, "accounts.view_authuser")
        user = self.get_object(user_id)
        if not user:
            return Response({"detail": "User account not found."}, status=404)
        return Response(_user_payload(user, request.user))

    def patch(self, request, user_id):
        self._require_owner(request)
        user = self.get_object(user_id)
        if not user:
            return Response({"detail": "User account not found."}, status=404)
        if user.is_superuser and user.pk != request.user.pk:
            return Response({"detail": "Other platform-owner accounts must be managed through secure server administration."}, status=409)
        serializer = AdminUserUpdateSerializer(data=request.data, partial=True, context={"user": user})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if user.pk == request.user.pk and data.get("is_active") is False:
            return Response({"detail": "You cannot suspend your own account."}, status=409)
        changes = {}
        with transaction.atomic():
            changed_fields = []
            for field in ("email", "first_name", "last_name", "is_active"):
                if field in data and getattr(user, field) != data[field]:
                    changes[field] = {"from": getattr(user, field), "to": data[field]}
                    setattr(user, field, data[field])
                    changed_fields.append(field)
            if "email_verified" in data:
                verified_at = timezone.now() if data["email_verified"] else None
                if (user.email_verified_at is not None) != data["email_verified"]:
                    changes["email_verified"] = {"from": user.email_verified_at is not None, "to": data["email_verified"]}
                    user.email_verified_at = verified_at
                    changed_fields.append("email_verified_at")
            if changed_fields:
                user.save(update_fields=changed_fields)
                UserAdministrationEvent.objects.create(
                    actor=request.user, target=user, target_user_id=user.pk,
                    target_email=user.email, action="updated", changes=changes,
                )
        return Response(_user_payload(user, request.user))

    def delete(self, request, user_id):
        self._require_owner(request)
        user = self.get_object(user_id)
        if not user:
            return Response({"detail": "User account not found."}, status=404)
        if user.pk == request.user.pk:
            return Response({"detail": "You cannot delete your own account."}, status=409)
        blockers = _user_delete_blockers(user)
        if blockers:
            return Response({"detail": "This account cannot be deleted safely.", "blockers": blockers}, status=409)
        target_id, target_email = user.pk, user.email
        with transaction.atomic():
            UserAdministrationEvent.objects.create(
                actor=request.user, target=None, target_user_id=target_id,
                target_email=target_email, action="deleted", changes={},
            )
            user.delete()
        return Response(status=204)


class OrderListView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "orders.view_order")
        queryset = Order.objects.select_related("store").prefetch_related("items")
        status_value = request.query_params.get("status", "").strip()
        search = request.query_params.get("search", "").strip()
        store_slug = request.query_params.get("store", "").strip()
        if status_value:
            queryset = queryset.filter(status=status_value)
        if store_slug:
            queryset = queryset.filter(store__slug=store_slug)
        if search:
            queryset = queryset.filter(
                Q(order_id__icontains=search)
                | Q(customer_email__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(store__name__icontains=search)
                | Q(store__slug__icontains=search)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([_serialize_order(order, detailed=True) for order in page])


class PaymentListView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "payments.view_paymenttransaction")
        queryset = PaymentTransaction.objects.select_related("store", "order")
        status_value = request.query_params.get("status", "").strip()
        search = request.query_params.get("search", "").strip()
        if status_value:
            queryset = queryset.filter(status=status_value)
        if search:
            queryset = queryset.filter(
                Q(order__order_id__icontains=search)
                | Q(order__customer_email__icontains=search)
                | Q(store__name__icontains=search)
                | Q(store__slug__icontains=search)
                | Q(provider_token__icontains=search)
                | Q(provider_transaction_id__icontains=search)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        results = [{
            "id": item.id,
            "order_id": str(item.order.order_id),
            "store": {"id": item.store_id, "name": item.store.name, "slug": item.store.slug},
            "customer_email": item.order.customer_email,
            "amount": _decimal(item.amount),
            "currency": item.currency,
            "status": item.status,
            "provider": item.provider,
            "provider_reference": item.provider_transaction_id or item.provider_token,
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        } for item in page]
        return paginator.get_paginated_response(results)


class PlatformGatewayListView(StaffAPIView):
    def get(self, request):
        if not (
            request.user.has_perm("payments.view_platformpaymentgateway")
            or request.user.has_perm("subscriptions.view_subscriptionpayment")
        ):
            raise PermissionDenied("You do not have access to payment gateway settings.")
        return Response({"gateways": platform_gateway_descriptors()})


class PlatformGatewayDetailView(StaffAPIView):
    def patch(self, request, code):
        if not (
            request.user.has_perm("payments.change_platformpaymentgateway")
            or request.user.has_perm("subscriptions.change_subscriptionplan")
        ):
            raise PermissionDenied("You do not have access to change payment gateway settings.")
        enabled = request.data.get("enabled")
        sandbox_enabled = request.data.get("sandbox_enabled")
        if enabled is None and sandbox_enabled is None:
            return Response({"detail": "Provide a gateway setting to update."}, status=400)
        if enabled is not None and not isinstance(enabled, bool):
            return Response({"enabled": ["Provide true or false."]}, status=400)
        if sandbox_enabled is not None and not isinstance(sandbox_enabled, bool):
            return Response({"sandbox_enabled": ["Provide true or false."]}, status=400)
        descriptor = next(
            (item for item in platform_gateway_descriptors() if item["code"] == code),
            None,
        )
        if descriptor is None:
            return Response({"detail": "Unknown payment gateway."}, status=404)
        if enabled and not descriptor["configured"]:
            return Response({"detail": "Configure this gateway before enabling it."}, status=409)
        if sandbox_enabled is not None and not descriptor["sandbox_configurable"]:
            return Response({"detail": "Sandbox mode is fixed for this gateway adapter."}, status=409)
        setting, _ = PlatformPaymentGateway.objects.get_or_create(
            code=code,
            defaults={"is_enabled": descriptor["enabled"], "sandbox_enabled": descriptor["sandbox"]},
        )
        if enabled is not None:
            setting.is_enabled = enabled
        if sandbox_enabled is not None:
            setting.sandbox_enabled = sandbox_enabled
        setting.updated_by = request.user
        setting.save(update_fields=["is_enabled", "sandbox_enabled", "updated_by", "updated_at"])
        updated = next(item for item in platform_gateway_descriptors() if item["code"] == code)
        return Response(updated)


class MembershipPaymentListView(StaffAPIView):
    """Membership checkout ledger, separate from merchants' store payments."""

    def get(self, request):
        self.require_permission(request, "subscriptions.view_subscriptionpayment")
        queryset = SubscriptionPayment.objects.select_related("user", "plan", "selected_store")
        status_value = request.query_params.get("status", "").strip()
        search = request.query_params.get("search", "").strip()
        if status_value:
            queryset = queryset.filter(status=status_value)
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search)
                | Q(plan_name__icontains=search)
                | Q(provider_transaction_id__icontains=search)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([{
            "id": str(item.pk),
            "merchant_email": item.user.email,
            "plan_name": item.plan_name,
            "plan_id": item.plan_id,
            "selected_store": ({
                "id": item.selected_store_id,
                "name": item.selected_store.name,
                "slug": item.selected_store.slug,
            } if item.selected_store_id else None),
            "entitlement_scope": item.entitlement_scope,
            "gross_amount": _decimal(item.gross_amount),
            "amount": _decimal(item.amount),
            "balance_applied": _decimal(item.credit_reserved),
            "credit_after_confirmation": _decimal(item.proration_credit),
            "currency": item.currency,
            "status": item.status,
            "provider": item.provider,
            "provider_reference": item.provider_transaction_id or item.provider_token,
            "processed_at": _iso(item.processed_at),
            "created_at": _iso(item.created_at),
            "updated_at": _iso(item.updated_at),
        } for item in page])


class DeploymentListView(StaffAPIView):
    def get(self, request):
        self.require_permission(request, "storefront_factory.view_storefrontrequest")
        queryset = StorefrontRequest.objects.select_related("store")
        status_value = request.query_params.get("status", "").strip()
        search = request.query_params.get("search", "").strip()
        if status_value:
            queryset = queryset.filter(deploy_status=status_value)
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(slug__icontains=search)
                | Q(design__icontains=search)
                | Q(deployed_url__icontains=search)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response([_serialize_deployment(item, detailed=True) for item in page])


class DeploymentEventsView(StaffAPIView):
    """Stream merchant-safe deployment progress to authorized platform staff."""

    renderer_classes = [EventStreamRenderer]

    def get(self, request, deployment_id):
        self.require_permission(request, "storefront_factory.view_storefrontrequest")
        deployment = StorefrontRequest.objects.filter(pk=deployment_id).first()
        if deployment is None:
            return Response({"detail": "Storefront not found."}, status=404)
        supplied_event_id = request.headers.get("Last-Event-ID") or request.query_params.get("after")
        try:
            last_event_id = max(0, int(supplied_event_id)) if supplied_event_id else max(0, deployment.event_version - 1)
        except (TypeError, ValueError):
            last_event_id = max(0, deployment.event_version - 1)
        single_pass = request.query_params.get("once") == "1"

        def sync_stream():
            nonlocal last_event_id
            cycles = 0
            while cycles < 25:
                events = _deployment_events_after(deployment.pk, last_event_id)
                for event in events:
                    last_event_id = event.version
                    yield _deployment_event_chunk(event)
                if cycles % 5 == 0:
                    _reconcile_deployment_truth(deployment.pk)
                if single_pass:
                    break
                if not events and cycles % 10 == 0:
                    yield ": keep-alive\n\n"
                cycles += 1
                time.sleep(1)

        async def async_stream():
            nonlocal last_event_id
            cycles = 0
            while cycles < 25:
                events = await sync_to_async(_deployment_events_after, thread_sensitive=True)(deployment.pk, last_event_id)
                for event in events:
                    last_event_id = event.version
                    yield _deployment_event_chunk(event)
                if cycles % 5 == 0:
                    await sync_to_async(_reconcile_deployment_truth, thread_sensitive=True)(deployment.pk)
                if single_pass:
                    break
                if not events and cycles % 10 == 0:
                    yield ": keep-alive\n\n"
                cycles += 1
                await asyncio.sleep(1)

        django_request = getattr(request, "_request", request)
        stream = async_stream() if isinstance(django_request, ASGIRequest) else sync_stream()
        response = StreamingHttpResponse(stream, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache, no-transform"
        response["X-Accel-Buffering"] = "no"
        return response


class DeploymentReleaseView(StaffAPIView):
    """Queue one controlled storefront update while its current release stays live."""

    def post(self, request, deployment_id):
        self.require_permission(request, "storefront_factory.change_storefrontrequest")
        revision = approved_release_revision()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            return Response(
                {
                    "code": "storefront_release_unavailable",
                    "detail": "A storefront update is not ready yet.",
                },
                status=503,
            )

        with transaction.atomic():
            deployment = (
                StorefrontRequest.objects.select_for_update()
                .filter(pk=deployment_id)
                .first()
            )
            if deployment is None:
                return Response({"detail": "Storefront not found."}, status=404)
            if storefront_release_status(deployment) == "current":
                return Response(
                    {"code": "storefront_already_current", "detail": "This storefront is already up to date."},
                    status=409,
                )
            from storefront_factory.repository_guards import migration_in_progress
            if migration_in_progress(deployment.pk):
                return Response(
                    {"code": "storefront_migration_active", "detail": "Finish the repository migration before updating this storefront."},
                    status=409,
                )
            from store.design_access import LEGACY_DESIGN_KEYS
            if deployment.design in LEGACY_DESIGN_KEYS:
                return Response(
                    {
                        "code": "storefront_redesign_required",
                        "detail": (
                            "This storefront uses a retired design. Choose a ready "
                            "business family from the merchant Design area before updating it."
                        ),
                    },
                    status=409,
                )
            has_live_release = bool(
                deployment.deployed_url
                and deployment.vercel_project_id
                and (
                    deployment.last_known_good_revision
                    or deployment.deployed_revision
                    or deployment.github_commit_sha
                )
            )
            is_currently_live = (
                deployment.pipeline_state == StorefrontRequest.PIPELINE_LIVE
                and deployment.deploy_status == StorefrontRequest.DEPLOY_DEPLOYED
            )
            is_retryable_update_failure = (
                deployment.pipeline_state == StorefrontRequest.PIPELINE_FAILED
                and deployment.deploy_status == StorefrontRequest.DEPLOY_FAILED
                and deployment.operation == StorefrontRequest.OPERATION_UPDATE
            )
            if not has_live_release or not (is_currently_live or is_retryable_update_failure):
                return Response(
                    {
                        "code": "storefront_not_ready",
                        "detail": "This storefront must be live before it can be updated.",
                    },
                    status=409,
                )
            deployment.last_known_good_revision = (
                deployment.deployed_revision
                or deployment.last_known_good_revision
                or deployment.github_commit_sha
            )
            deployment.last_known_good_deployment_id = (
                deployment.vercel_deployment_id
                or deployment.last_known_good_deployment_id
            )
            deployment.target_revision = revision
            deployment.operation = StorefrontRequest.OPERATION_UPDATE
            deployment.task_id = None
            deployment.attempt_count = 0
            deployment.log = ""
            deployment.deploy_log = ""
            deployment.internal_error = ""
            deployment.github_commit_sha = ""
            deployment.vercel_deployment_id = ""
            deployment.started_at = None
            deployment.last_heartbeat_at = None
            deployment.completed_at = None
            deployment.save(update_fields=[
                "last_known_good_revision",
                "last_known_good_deployment_id",
                "target_revision",
                "operation",
                "task_id",
                "attempt_count",
                "log",
                "deploy_log",
                "internal_error",
                "github_commit_sha",
                "vercel_deployment_id",
                "started_at",
                "last_heartbeat_at",
                "completed_at",
            ])
            deployment = record_deployment_state(
                deployment.pk,
                StorefrontRequest.PIPELINE_ACCEPTED,
                message="deployment.accepted",
                reset=True,
            )
            queue_token = vercel_oidc_token(request)
            transaction.on_commit(
                lambda deployment_id=deployment.pk, token=queue_token: (
                    enqueue_storefront_deployment(
                        deployment_id,
                        force=True,
                        oidc_token=token,
                    )
                )
            )

        return Response(_serialize_deployment(deployment, detailed=True), status=202)


class SystemHealthView(StaffAPIView):
    def get(self, request):
        started = monotonic()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        database_latency_ms = round((monotonic() - started) * 1000, 1)

        providers = {
            "vercel": bool(settings.VERCEL_API_TOKEN),
            "cloudflare": bool(settings.CLOUDFLARE_API_TOKEN and settings.CLOUDFLARE_ZONE_ID),
            "github": bool(
                (settings.STOREFRONT_SOURCE_TOKEN or settings.GITHUB_TOKEN)
                and settings.DESIGN_STOCK_REPO
                and (settings.STOREFRONT_DEPLOYMENT_TOKEN if settings.STOREFRONT_NEW_REPO_ENABLED and settings.STOREFRONT_DEPLOYMENT_REPO
                     and settings.STOREFRONT_DEPLOYMENT_REPO != settings.DESIGN_STOCK_REPO else settings.GITHUB_TOKEN)
            ),
            "cloudinary": bool(settings.CLOUDINARY_STORAGE.get("CLOUD_NAME")),
            "platform_payment_gateway": _subscription_provider_status()["configured"],
        }
        catalog = {
            "products": Product.objects.count(),
            "variants": StockVariant.objects.count(),
            "active_without_variants": Product.objects.filter(is_active=True, variants__isnull=True).count(),
            "out_of_stock_variants": StockVariant.objects.filter(
                product__is_active=True, quantity__lte=F("reserved")
            ).count(),
        }
        operations = {
            "untreated_stock_exceptions": QuantityException.objects.filter(treated=False).count(),
            "failed_deployments": StorefrontRequest.objects.filter(
                deploy_status=StorefrontRequest.DEPLOY_FAILED
            ).count(),
            "stalled_deployments": StorefrontRequest.objects.filter(
                pipeline_state__in=[
                    StorefrontRequest.PIPELINE_ACCEPTED,
                    StorefrontRequest.PIPELINE_PREPARING,
                    StorefrontRequest.PIPELINE_PUBLISHING,
                    StorefrontRequest.PIPELINE_PROVISIONING,
                    StorefrontRequest.PIPELINE_CONFIGURING_DOMAIN,
                    StorefrontRequest.PIPELINE_BUILDING,
                    StorefrontRequest.PIPELINE_VERIFYING,
                ],
                last_heartbeat_at__lt=timezone.now() - timedelta(minutes=6),
            ).count(),
            "failed_campaigns": NewsletterCampaign.objects.filter(
                status=NewsletterCampaign.Status.FAILED
            ).count(),
            "active_subscribers": Subscriber.objects.filter(status=Subscriber.Status.ACTIVE).count(),
        }
        attention_count = (
            sum(1 for configured in providers.values() if not configured)
            + catalog["active_without_variants"]
            + operations["untreated_stock_exceptions"]
            + operations["failed_deployments"]
            + operations["stalled_deployments"]
            + operations["failed_campaigns"]
        )

        return Response({
            "generated_at": timezone.now().isoformat(),
            "status": "attention" if attention_count else "healthy",
            "attention_count": attention_count,
            "database": {
                "status": "healthy",
                "vendor": connection.vendor,
                "latency_ms": database_latency_ms,
            },
            "providers": providers,
            "catalog": catalog,
            "operations": operations,
        })


def _serialize_store(store):
    owner = next((member.user.email for member in store.members.all() if member.role == "owner"), None)
    config = getattr(store, "config", None)
    return {
        "id": store.id,
        "name": store.name,
        "slug": store.slug,
        "owner_email": owner,
        "retail_type": store.retail_type.label if store.retail_type else "Unconfigured",
        "retail_type_key": store.retail_type.key if store.retail_type else None,
        "currency": config.currency if config else "MAD",
        "plan": config.plan if config else "free",
        "design": config.design if config else None,
        "domain": config.domain if config else "",
        "created_at": _iso(store.created_at),
    }


def _serialize_order(order, detailed=False):
    payload = {
        "id": order.id,
        "order_id": str(order.order_id),
        "store": {"id": order.store_id, "name": order.store.name, "slug": order.store.slug},
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "amount": _decimal(order.amount),
        "currency": order.currency,
        "status": order.status,
        "payment_mode": order.payment_mode,
        "payment_status": order.is_paid,
        "exception": order.exception,
        "created_at": _iso(order.created_at),
    }
    if detailed:
        payload.update({
            "items_count": sum(item.quantity for item in order.items.all()),
            "customer_city": order.customer_city,
            "updated_at": _iso(order.updated_at),
        })
    return payload


def _serialize_deployment(item, detailed=False):
    payload = {
        "id": item.id,
        "store": (
            {"id": item.store_id, "name": item.store.name, "slug": item.store.slug}
            if item.store_id else None
        ),
        "name": item.name,
        "slug": item.slug,
        "design": item.design,
        "status": item.status,
        "deploy_status": item.deploy_status,
        "pipeline_state": item.pipeline_state,
        "progress": item.progress,
        "public_message": item.public_message,
        "attempt_count": item.attempt_count,
        "operation": item.operation,
        "release_status": storefront_release_status(item),
        "last_heartbeat_at": _iso(item.last_heartbeat_at),
        "completed_at": _iso(item.completed_at),
        "deployed_url": item.deployed_url,
        "created_at": _iso(item.created_at),
    }
    if detailed:
        error = item.deploy_log or item.log
        payload.update({
            "domain": item.domain,
            "vercel_project_id": item.vercel_project_id,
            "source_repository": item.source_repository,
            "deployment_repository": item.deployment_repository,
            "deployment_branch": item.deployment_branch,
            "deployment_app_path": item.deployment_app_path,
            "deployment_layout": item.deployment_layout,
            "github_commit_sha": item.github_commit_sha,
            "target_revision": item.target_revision,
            "deployed_revision": item.deployed_revision,
            "last_known_good_revision": item.last_known_good_revision,
            "last_error": error[-500:] if error and item.deploy_status == StorefrontRequest.DEPLOY_FAILED else "",
        })
    return payload
