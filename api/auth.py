"""Tenant-scoped storefront customer identity and profile endpoints."""

from datetime import timedelta
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.authentication import ClientCookieJWTAuthentication
from accounts.cookies import clear_client_cookies, resolve_tenant, set_auth_cookies
from accounts.customer_email_verification import (
    InvalidCustomerEmailVerificationToken,
    build_customer_email_verification_url,
    verify_customer_email,
)
from accounts.password_recovery import issue_recovery, consume_recovery, customer_reset_url, normalized_recovery_email
from accounts.models import AuthUser
from accounts.tokens import CUSTOMER_SCOPE, issue_scoped_tokens
from api.auth_serializers import CustomerProfileSerializer, CustomerRegistrationSerializer
from store.models import ClientProfile, Store, StoreCustomer


logger = logging.getLogger(__name__)
LOGIN_FAILURE = {"detail": "Unable to sign in with the provided information.", "code": "signin_unavailable"}
RESEND_NEUTRAL = {"message": "If the account is eligible, a confirmation email will be sent."}


def _store_for_request(request):
    slug = resolve_tenant(request)
    return Store.objects.select_related("config").filter(slug=slug).first() if slug else None


def _customer(profile, store_customer=None):
    user = profile.user
    return {
        "id": profile.id,
        "email": user.email,
        "emailVerified": user.email_verified_at is not None,
        "firstName": user.first_name,
        "lastName": user.last_name,
        "phone": str(profile.phone),
        "phoneVerified": profile.is_phone_verified,
        "address": profile.address,
        "city": profile.city,
        "country": profile.country,
        "preferredLanguage": profile.preferred_language,
        "loyaltyPoints": store_customer.loyalty_points if store_customer else profile.loyalty_points,
        "createdAt": profile.created_at,
    }


def _link_store(profile, store):
    profile.stores.add(store)
    customer, _ = StoreCustomer.objects.get_or_create(profile=profile, store=store)
    return customer


def _set_client_cookies(response, user, request):
    tenant = resolve_tenant(request)
    if tenant:
        access, refresh = issue_scoped_tokens(user, CUSTOMER_SCOPE, tenant=tenant)
        set_auth_cookies(response, "client", access, refresh, tenant=tenant)
    return response


def _send_customer_verification(user, store):
    from notifications.platform_email import send_customer_verification

    verification_url = build_customer_email_verification_url(user, store)
    send_customer_verification(user, store, verification_url)


class CustomerCSRFView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "customer_registration"

    def post(self, request):
        store = _store_for_request(request)
        if not store:
            return Response({"detail": "This store is not available."}, status=status.HTTP_404_NOT_FOUND)
        serializer = CustomerRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if AuthUser.objects.filter(email__iexact=serializer.validated_data["email"]).exists():
            return Response(
                {"detail": "Unable to create this account.", "code": "account_exists"},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            with transaction.atomic():
                profile = serializer.save()
                _link_store(profile, store)
        except IntegrityError:
            return Response(
                {"detail": "Unable to create this account.", "code": "account_exists"},
                status=status.HTTP_409_CONFLICT,
            )
        transaction.on_commit(lambda: _send_customer_verification(profile.user, store))
        return Response(
            {
                "message": "Account created. Check your email to continue.",
                "verificationRequired": True,
                "email": profile.user.email,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "customer_login"

    def post(self, request):
        store = _store_for_request(request)
        email = str(request.data.get("email", "")).strip().lower()
        password = str(request.data.get("password", ""))
        if not store or not email or not password:
            return Response(LOGIN_FAILURE, status=status.HTTP_401_UNAUTHORIZED)
        user = AuthUser.objects.filter(email__iexact=email).first()
        if not user or not user.check_password(password):
            return Response(LOGIN_FAILURE, status=status.HTTP_401_UNAUTHORIZED)
        if user.is_staff or user.is_superuser:
            # Platform work identities never cross into merchant/customer apps.
            return Response(LOGIN_FAILURE, status=status.HTTP_401_UNAUTHORIZED)

        profile, _ = ClientProfile.objects.get_or_create(user=user)
        store_customer = _link_store(profile, store)
        if user.email_verified_at is None:
            return Response(
                {
                    "detail": "Account confirmation is required.",
                    "code": "verification_required",
                    "verificationRequired": True,
                    "email": user.email,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if not user.is_active:
            return Response(LOGIN_FAILURE, status=status.HTTP_401_UNAUTHORIZED)
        response = Response({"customer": _customer(profile, store_customer)})
        return _set_client_cookies(response, user, request)


class VerifyCustomerEmailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        token = str(request.data.get("token", "")).strip()
        try:
            user, store_customer, activated = verify_customer_email(
                token,
                expected_store_slug=resolve_tenant(request) or "",
            )
        except InvalidCustomerEmailVerificationToken as exc:
            logger.info("Customer email verification rejected: %s", exc.reason)
            return Response(
                {"detail": "This confirmation link is no longer available.", "code": "verification_unavailable"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if resolve_tenant(request) != store_customer.store.slug:
            return Response(
                {"detail": "This confirmation link is no longer available.", "code": "verification_unavailable"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        response = Response({
            "message": "Email address confirmed.",
            "verified": True,
            "alreadyVerified": not activated,
            "customer": _customer(store_customer.profile, store_customer),
        })
        return _set_client_cookies(response, user, request)


class ResendCustomerVerificationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "customer_email_verification"

    def post(self, request):
        store = _store_for_request(request)
        email = str(request.data.get("email", "")).strip().lower()
        if not store or not email:
            return Response(RESEND_NEUTRAL)
        now = timezone.now()
        eligible = AuthUser.objects.filter(
            email__iexact=email,
            email_verified_at__isnull=True,
            client_profile__store_customers__store=store,
        ).filter(
            Q(email_verification_sent_at__isnull=True)
            | Q(email_verification_sent_at__lte=now - timedelta(seconds=settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS))
        )
        user = eligible.select_related("client_profile").first()
        if user and eligible.filter(pk=user.pk).update(email_verification_sent_at=now):
            transaction.on_commit(lambda: _send_customer_verification(user, store))
        return Response(RESEND_NEUTRAL)


class MeView(APIView):
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer = StoreCustomer.objects.select_related("profile__user").get(
            profile__user=request.user,
            store__slug=resolve_tenant(request),
        )
        return Response({"customer": _customer(customer.profile, customer)})


class CustomerProfileView(APIView):
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        customer = StoreCustomer.objects.select_related("profile__user").get(
            profile__user=request.user,
            store__slug=resolve_tenant(request),
        )
        serializer = CustomerProfileSerializer(customer.profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        return Response({"customer": _customer(profile, customer)})


class LogoutView(APIView):
    authentication_classes = [ClientCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        response = Response({"message": "Logged out."})
        tenant = resolve_tenant(request)
        if tenant:
            clear_client_cookies(response, tenant)
        return response


class CustomerPasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "customer_password_reset"

    def post(self, request):
        store = _store_for_request(request)
        email = normalized_recovery_email(request.data.get("email"))
        if not email:
            return Response({"email": ["Enter a valid email address."]}, status=status.HTTP_400_BAD_REQUEST)
        if store:
            customer = StoreCustomer.objects.select_related("profile__user").filter(
                store=store, profile__user__email__iexact=email,
                profile__user__is_active=True,
                profile__user__email_verified_at__isnull=False,
            ).first()
            if customer:
                from notifications.platform_email import send_customer_password_reset

                user = customer.profile.user
                raw = issue_recovery(user, "customer", store)
                if raw:
                    reset_url = customer_reset_url(raw, store)
                    transaction.on_commit(lambda: send_customer_password_reset(user, store, reset_url))
        return Response({"message": "If an eligible account exists, a recovery email will be sent."})


class CustomerPasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "customer_password_reset_confirm"

    def post(self, request):
        store = _store_for_request(request)
        token = str(request.data.get("token", ""))
        password = str(request.data.get("password", ""))
        if not store or not token:
            return Response(
                {"code": "reset_link_invalid", "detail": "This recovery link is unavailable."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = consume_recovery(token, password, "customer", store)
        if result == "password":
            return Response(
                {"code": "password_invalid", "password": ["Choose a less common password that differs from your account details."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if result != "ok":
            return Response(
                {"code": "reset_link_invalid", "detail": "This recovery link is unavailable."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"message": "Password updated. You can sign in now."})
