from datetime import timedelta
from dataclasses import replace
import logging

from django.shortcuts import get_object_or_404
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import OperationalError, close_old_connections
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.cache import patch_cache_control
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import OwnerCookieJWTAuthentication
from store.access import IsMerchantAccount
from payments.gateways import get_gateway
from payments.gateways.registry import configured_backends
from payments.gateways.availability import available_platform_gateways, effective_platform_credentials

from .models import EntitlementDefinition, PlanEntitlement, SubscriptionCreditAccount, SubscriptionPayment, SubscriptionPlan, UserSubscription
from .serializers import CheckoutSerializer, SubscriptionPlanSerializer
from .services import InvalidSubscriptionWebhook, PaymentCaptureError, PendingCheckoutConflict, ProviderVerificationUnavailable, SubscriptionError, capture_subscription_payment, create_subscription_checkout, ensure_free_subscription, expire_stale_subscription_checkouts, handle_subscription_webhook, refresh_expired_subscription, verify_provider_transaction
from .access import user_has_active_subscription


logger = logging.getLogger(__name__)


class PlanCatalogView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            gateways = available_platform_gateways()
        except Exception:
            gateways = []
        billing = next(
            (item for item in gateways if item["code"] == settings.PLATFORM_PAYMENT_GATEWAY),
            gateways[0] if gateways else {
                "code": settings.PLATFORM_PAYMENT_GATEWAY,
                "name": "Payment provider",
                "configured": False,
                "sandbox": False,
            },
        )
        definitions = list(EntitlementDefinition.objects.filter(is_active=True))
        plans = SubscriptionPlan.objects.filter(is_active=True).prefetch_related(
            Prefetch(
                "configured_entitlements",
                queryset=PlanEntitlement.objects.filter(definition__is_active=True).select_related("definition"),
            ),
        )
        response = Response({
            "plans": SubscriptionPlanSerializer(
                plans,
                many=True,
                context={"entitlement_definitions": definitions},
            ).data,
            "billing": {
                "available": billing["configured"],
                "sandbox": billing["sandbox"],
                "provider": {"code": billing["code"], "name": billing["name"]},
                "gateways": [{
                    "code": item["code"],
                    "name": item["name"],
                    "sandbox": item["sandbox"],
                } for item in gateways],
            },
        })
        patch_cache_control(
            response,
            public=True,
            max_age=60,
            s_maxage=300,
            stale_while_revalidate=600,
        )
        return response


class SubscriptionStatusView(APIView):
    authentication_classes = [OwnerCookieJWTAuthentication]
    permission_classes = [IsAuthenticated, IsMerchantAccount]

    def dispatch(self, request, *args, **kwargs):
        """Retry this read-only status request once after a dropped DB connection.

        Hosted PostgreSQL poolers can occasionally close a connection while a
        dashboard is polling after checkout. Replaying this GET is safe and
        prevents a transient disconnect from being presented as a failed
        membership upgrade. A second failure is deliberately allowed through
        so persistent database outages remain observable.
        """
        try:
            return super().dispatch(request, *args, **kwargs)
        except OperationalError:
            logger.warning("Retrying subscription status after a transient database disconnect.")
            close_old_connections()
            return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        expire_stale_subscription_checkouts(request.user)
        subscriptions = list(UserSubscription.objects.select_related("plan", "selected_store").filter(user=request.user))
        for item in subscriptions:
            refresh_expired_subscription(item)
        ensure_free_subscription(request.user)
        subscriptions = list(UserSubscription.objects.select_related("plan", "selected_store").filter(user=request.user))
        subscriptions.sort(
            key=lambda item: (item.status == UserSubscription.Status.ACTIVE, item.current_period_end or item.updated_at),
            reverse=True,
        )
        subscription = subscriptions[0] if subscriptions else None
        access_allowed = user_has_active_subscription(request.user)
        latest_payment = SubscriptionPayment.objects.filter(user=request.user).first()
        requested_payment = None
        payment_id = request.query_params.get("payment_id", "").strip()
        if payment_id:
            try:
                requested_payment = SubscriptionPayment.objects.filter(
                    user=request.user, pk=payment_id,
                ).first()
            except (ValueError, TypeError):
                requested_payment = None
        return Response({
            "access": {
                "allowed": access_allowed,
                "code": None if access_allowed else "subscription_required",
            },
            "subscription": _subscription_payload(subscription),
            "subscriptions": [_subscription_payload(item) for item in subscriptions],
            "latest_payment": _payment_payload(latest_payment),
            "payment": _payment_payload(requested_payment),
            "balances": [
                {
                    "currency": account.currency,
                    "balance": str(account.balance),
                    "reserved": str(account.reserved),
                    "available": str(account.available),
                }
                for account in SubscriptionCreditAccount.objects.filter(user=request.user).order_by("currency")
            ],
        })


class SubscriptionCheckoutView(APIView):
    authentication_classes = [OwnerCookieJWTAuthentication]
    permission_classes = [IsAuthenticated, IsMerchantAccount]

    def post(self, request):
        data = request.data.copy()
        header_key = request.headers.get("Idempotency-Key", "").strip()
        body_key = str(data.get("idempotency_key", "")).strip()
        if header_key and body_key and header_key != body_key:
            return Response(
                {"detail": "The checkout request could not be verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if header_key and not body_key:
            data["idempotency_key"] = header_key
        serializer = CheckoutSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        plan = get_object_or_404(SubscriptionPlan, pk=serializer.validated_data["plan_id"])
        try:
            payment, payment_url = create_subscription_checkout(
                request.user,
                plan,
                serializer.validated_data.get("store_id"),
                serializer.validated_data["lang"],
                serializer.validated_data.get("return_origin"),
                serializer.validated_data.get("idempotency_key"),
                use_balance=serializer.validated_data["use_balance"],
                provider=serializer.validated_data.get("provider"),
            )
        except PendingCheckoutConflict as exc:
            payment = exc.payment
            expires_at = payment.updated_at + timedelta(
                seconds=settings.SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS,
            )
            retry_after = max(0, int((expires_at - timezone.now()).total_seconds()))
            response = Response({
                "code": "checkout_in_progress",
                "detail": "A previous checkout is still active for this membership.",
                "checkout": {
                    "payment_id": str(payment.id),
                    "payment_url": payment.checkout_url,
                    "balance_authorized": payment.balance_authorized,
                    "expires_at": expires_at,
                    "retry_after_seconds": retry_after,
                },
            }, status=status.HTTP_409_CONFLICT)
            response["Retry-After"] = str(retry_after)
            return response
        except SubscriptionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except Exception:
            return Response(
                {"detail": "The payment provider is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({
            "payment_id": str(payment.id),
            "payment_url": payment_url,
            "gross_amount": str(payment.gross_amount),
            "balance_applied": str(payment.credit_reserved),
            "balance_authorized": payment.balance_authorized,
            "amount": str(payment.amount),
            "credit_after_confirmation": str(payment.proration_credit),
            "currency": payment.currency,
        }, status=status.HTTP_201_CREATED)


class PayPalOrderCreateView(SubscriptionCheckoutView):
    """Explicit PayPal Orders facade for the merchant dashboard."""

    def post(self, request):
        if settings.PLATFORM_PAYMENT_GATEWAY != "paypal":
            return Response(
                {"detail": "PayPal Sandbox billing is not enabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        response = super().post(request)
        if response.status_code == status.HTTP_201_CREATED:
            response.data.update({
                "paypal_order_id": SubscriptionPayment.objects.only("provider_token").get(
                    pk=response.data["payment_id"],
                ).provider_token,
                "approval_url": response.data.pop("payment_url"),
                "status": SubscriptionPayment.Status.PENDING,
            })
        return response


class PayPalOrderCaptureView(APIView):
    authentication_classes = [OwnerCookieJWTAuthentication]
    permission_classes = [IsAuthenticated, IsMerchantAccount]

    def post(self, request, paypal_order_id):
        if settings.PLATFORM_PAYMENT_GATEWAY != "paypal":
            return Response(
                {"detail": "PayPal Sandbox billing is not enabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            payment = capture_subscription_payment(request.user, paypal_order_id)
        except ProviderVerificationUnavailable:
            return Response(
                {"detail": "PayPal verification is temporarily unavailable.", "code": "provider_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except InvalidSubscriptionWebhook:
            return Response(
                {"detail": "The PayPal payment could not be verified.", "code": "verification_failed"},
                status=status.HTTP_409_CONFLICT,
            )
        except PaymentCaptureError as exc:
            provider_status = (
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.code in {"provider_timeout", "provider_unavailable", "configuration_missing"}
                else status.HTTP_409_CONFLICT
            )
            return Response({"detail": str(exc), "code": exc.code}, status=provider_status)
        except SubscriptionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except Exception:
            logger.exception("Local subscription activation failed after PayPal capture")
            return Response(
                {"detail": "Payment was received, but activation is pending reconciliation.", "code": "activation_pending"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({
            "payment_id": str(payment.pk),
            "paypal_order_id": payment.provider_token,
            "status": payment.status,
        })


class SubscriptionWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider=None):
        raw_body = request.body
        payload = request.data
        if not isinstance(payload, dict):
            return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
        provider_codes = [provider] if provider else [
            settings.PLATFORM_PAYMENT_GATEWAY,
            *(code for code in configured_backends() if code != settings.PLATFORM_PAYMENT_GATEWAY),
        ]
        gateway = event = payment = None
        signature_matched = False
        for provider_code in provider_codes:
            try:
                candidate = get_gateway(provider_code)
            except Exception:
                continue
            credentials = effective_platform_credentials(candidate)
            credential_options = [credentials]
            if "sandbox" in credentials:
                alternate = dict(credentials)
                alternate["sandbox"] = not credentials["sandbox"]
                credential_options.append(alternate)
            verified_credentials = next((
                option for option in credential_options
                if candidate.verify_webhook(raw_body, request.headers, option)
            ), None)
            if verified_credentials is None:
                continue
            signature_matched = True
            try:
                candidate_event = candidate.parse_webhook(payload)
                if "sandbox" in verified_credentials:
                    candidate_event = replace(
                        candidate_event,
                        sandbox=bool(verified_credentials["sandbox"]),
                    )
            except Exception:
                return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                candidate_payment = SubscriptionPayment.objects.get(pk=candidate_event.order_id)
            except (SubscriptionPayment.DoesNotExist, ValueError, DjangoValidationError):
                candidate_payment = SubscriptionPayment.objects.filter(
                    provider=candidate.code,
                    provider_token=candidate_event.provider_token,
                ).first()
                if candidate_payment is None:
                    return Response({"received": True})
            if candidate_payment.provider != candidate.code:
                continue
            gateway, event, payment = candidate, candidate_event, candidate_payment
            break
        if gateway is None or event is None or payment is None:
            if signature_matched:
                return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"received": False, "error": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            if (
                gateway.code == "paypal"
                and event.event_name == "CHECKOUT.ORDER.APPROVED"
                and payment.status == SubscriptionPayment.Status.PENDING
            ):
                # PayPal approval is only permission to capture; it never
                # grants access by itself. Capture server-side from this
                # verified webhook, then let the normal completed-capture
                # settlement path verify and activate exactly once.
                payment = capture_subscription_payment(
                    payment.user,
                    payment.provider_token,
                )
            if (
                event.outcome == "paid"
                and payment.status not in {
                    SubscriptionPayment.Status.CONFIRMED,
                    SubscriptionPayment.Status.CANCELED,
                }
            ):
                verify_provider_transaction(payment, event)
            handle_subscription_webhook(payment, event)
        except ProviderVerificationUnavailable:
            # A transient 5xx asks the provider to retry; no access is granted
            # until its own transaction API confirms the purchase.
            return Response({"received": False}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except InvalidSubscriptionWebhook:
            return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
        except SubscriptionError:
            # PayPal should retry approval notifications when capture is
            # temporarily unable to reach a final local state.
            if gateway.code == "paypal" and event.event_name == "CHECKOUT.ORDER.APPROVED":
                return Response({"received": False}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            return Response({"received": False, "error": "Invalid payment event."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"received": True})


def _subscription_payload(subscription):
    if not subscription:
        return None
    return {
        "status": subscription.status,
        "plan": SubscriptionPlanSerializer(subscription.plan).data if subscription.plan else None,
        "plan_revision": subscription.plan_revision,
        "entitlements": subscription.entitlements_snapshot,
        "selected_store": ({"id": subscription.selected_store_id, "name": subscription.selected_store.name, "slug": subscription.selected_store.slug} if subscription.selected_store_id else None),
        "started_at": subscription.started_at,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": subscription.cancel_at_period_end,
    }


def _payment_payload(payment):
    if not payment:
        return None
    return {
        "id": str(payment.id),
        "plan_id": payment.plan_id,
        "plan_revision": payment.plan_revision,
        "plan_name": payment.plan_name,
        "gross_amount": str(payment.gross_amount),
        "amount": str(payment.amount),
        "balance_applied": str(payment.credit_reserved),
        "balance_authorized": payment.balance_authorized,
        "credit_after_confirmation": str(payment.proration_credit),
        "currency": payment.currency,
        "provider": payment.provider,
        "status": payment.status,
        "entitlement_scope": payment.entitlement_scope,
        "selected_store_id": payment.selected_store_id,
        "processed_at": payment.processed_at,
        "created_at": payment.created_at,
    }
