from datetime import timedelta
import logging
from secrets import compare_digest
from urllib.parse import urlencode

from rest_framework import generics, status
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView
from django.middleware.csrf import get_token
from django.shortcuts import redirect

from config.origins import frontend_url_for_origin

from .authentication import OwnerCookieJWTAuthentication, _enforce_csrf
from .access import has_merchant_access
from .cookies import (
    SCOPE_ORDER,
    clear_backoffice_cookies,
    clear_client_cookies,
    refresh_cookie_names,
    resolve_tenant,
    set_auth_cookies,
)
from .email_verification import (
    InvalidEmailVerificationToken,
    build_email_verification_url,
    verify_merchant_email,
)
from .serializers import MerchantRegistrationSerializer
from .tokens import CUSTOMER_SCOPE, MERCHANT_SCOPE, PLATFORM_SCOPE, issue_scoped_tokens
from .password_recovery import issue_recovery, consume_recovery, merchant_reset_url, normalized_recovery_email


LOGIN_FAILURE = {'detail': 'Unable to sign in with the provided information.'}
logger = logging.getLogger(__name__)


def _request_dashboard_url(request, *, verification=False):
    """Keep local navigation on the host the browser actually opened."""
    return frontend_url_for_origin(
        request.headers.get("Origin"),
        app="dashboard",
        fallback=(
            settings.EMAIL_VERIFICATION_FRONTEND_URL
            if verification
            else settings.MERCHANT_DASHBOARD_URL
        ),
        allow_local=settings.DEBUG,
        path="/verify-email" if verification else "",
    )


def _login_failure_response():
    """Do not reveal whether credentials, status, role, or app access failed."""
    return Response(LOGIN_FAILURE, status=status.HTTP_401_UNAUTHORIZED)


def _user_payload(u):
    payload = {
        'email':      u.email,
        'first_name': u.first_name,
        'last_name':  u.last_name,
        'email_verified': u.email_verified_at is not None,
    }
    if hasattr(u, 'merchant_profile'):
        payload.update({
            'business_name': u.merchant_profile.business_name,
            'phone_number': str(u.merchant_profile.phone_number),
            'phone_verified': u.merchant_profile.is_phone_verified,
            'preferred_language': u.merchant_profile.preferred_language,
        })
    return payload


class CSRFTokenView(APIView):
    """Issue a CSRF token for the browser before cookie-authenticated writes."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


class RegisterView(generics.CreateAPIView):
    """Self-service signup. Creates identity only — a merchant's store is
    created separately via POST /api/store/ (which also makes them its owner)."""
    serializer_class = MerchantRegistrationSerializer
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'merchant_registration'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            # Field names are operationally useful and contain no submitted
            # values. Keep values and exact validation messages out of logs.
            logger.info(
                "Merchant registration rejected fields=%s",
                ",".join(sorted(serializer.errors.keys())),
            )
            raise ValidationError(serializer.errors)
        try:
            user = serializer.save()
        except IntegrityError as exc:
            raise ValidationError({'email': 'Unable to use this email address.'}) from exc
        from notifications.platform_email import send_merchant_verification

        verification_url = build_email_verification_url(
            user,
            frontend_url=_request_dashboard_url(request, verification=True),
        )
        transaction.on_commit(lambda: send_merchant_verification(user, verification_url))
        return Response(
            {
                'message': 'Account created. Check your email to continue.',
                'verification_required': True,
                'user': _user_payload(user),
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyMerchantEmailView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        """Compatibility bridge for old emails; never verifies through GET."""
        query = urlencode({'token': request.query_params.get('token', '')})
        return redirect(f"{settings.EMAIL_VERIFICATION_FRONTEND_URL}?{query}")

    def post(self, request):
        try:
            user, activated = verify_merchant_email(str(request.data.get('token', '')).strip())
        except InvalidEmailVerificationToken as exc:
            # Preserve a useful server-side diagnostic without logging the
            # signed token, address, or account identifier.
            logger.info("Merchant email verification rejected: %s", exc.reason)
            return Response(
                {'detail': 'This verification link is invalid or no longer available.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if activated:
            from notifications.platform_email import send_merchant_space_ready

            # The activation transaction is already committed here. Send this
            # only for the first successful verification so link replays can
            # never produce duplicate onboarding messages.
            dashboard_url = _request_dashboard_url(request)
            transaction.on_commit(
                lambda: send_merchant_space_ready(user, dashboard_url=dashboard_url)
            )
        return Response({
            'message': 'Email address confirmed.',
            'verified': True,
            'already_verified': not activated,
        })


class ResendMerchantVerificationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'email_verification'

    def post(self, request):
        # Always return the same response so this endpoint cannot enumerate
        # registered merchant addresses.
        neutral = {'message': 'If the account is eligible, a verification email will be sent.'}
        email = str(request.data.get('email', '')).strip().lower()
        if not email:
            return Response(neutral)
        now = timezone.now()
        eligible = (
            get_user_model().objects.filter(
                email__iexact=email,
                email_verified_at__isnull=True,
                merchant_profile__isnull=False,
            )
            .filter(
                Q(email_verification_sent_at__isnull=True)
                | Q(
                    email_verification_sent_at__lte=now - timedelta(
                        seconds=settings.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS
                    )
                )
            )
        )
        user = eligible.select_related('merchant_profile').first()
        if user:
            from notifications.platform_email import send_merchant_verification

            # This conditional update is the cross-process claim; only one
            # concurrent request may schedule the message.
            if eligible.filter(pk=user.pk).update(email_verification_sent_at=now):
                verification_url = build_email_verification_url(
                    user,
                    frontend_url=_request_dashboard_url(request, verification=True),
                )
                transaction.on_commit(lambda: send_merchant_verification(user, verification_url))
        return Response(neutral)


class MerchantPasswordResetRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "merchant_password_reset"

    def post(self, request):
        email = normalized_recovery_email(request.data.get("email"))
        if not email:
            return Response({"email": ["Enter a valid email address."]}, status=status.HTTP_400_BAD_REQUEST)
        user = get_user_model().objects.filter(email__iexact=email).first()
        if user:
            raw = issue_recovery(user, "merchant")
            if raw:
                from notifications.platform_email import send_merchant_password_reset

                url = merchant_reset_url(raw, _request_dashboard_url(request))
                transaction.on_commit(lambda: send_merchant_password_reset(user, url))
        return Response({"message": "If an eligible account exists, a recovery email will be sent."})


class MerchantPasswordResetConfirmView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "merchant_password_reset_confirm"

    def post(self, request):
        result = consume_recovery(
            request.data.get("token"), str(request.data.get("password", "")), "merchant",
        )
        if result == "password":
            return Response(
                {"code": "password_invalid", "password": ["Choose a less common password that differs from your account details."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if result != "ok":
            return Response(
                {"code": "reset_link_invalid", "detail": "This recovery link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"message": "Password updated. You can sign in now."})


class CustomTokenObtainPairView(TokenObtainPairView):
    """Login with email + password. Sets role-scoped httpOnly JWT cookies.

    A ``scope`` body field (staff/owner/client) selects exactly one application
    audience. Multi-role identities must choose it explicitly so a platform
    session can never silently become a merchant or customer session.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except (AuthenticationFailed, ValidationError):
            return _login_failure_response()
        user = serializer.user
        tenant = resolve_tenant(request)
        requested = (request.data.get('scope') or '').strip().lower()
        aliases = {'platform': 'staff', 'merchant': 'owner', 'customer': 'client'}
        scope = aliases.get(requested, requested)

        eligible = []
        if user.is_staff or user.is_superuser:
            eligible.append('staff')
        if has_merchant_access(user):
            eligible.append('owner')
        if tenant and _is_store_customer(user, tenant):
            eligible.append('client')

        if scope:
            if scope not in SCOPE_ORDER or scope not in eligible:
                return _login_failure_response()
        elif len(eligible) == 1:
            scope = eligible[0]
        elif len(eligible) > 1:
            return _login_failure_response()
        else:
            return _login_failure_response()

        token_scope = {'staff': PLATFORM_SCOPE, 'owner': MERCHANT_SCOPE, 'client': CUSTOMER_SCOPE}[scope]
        access, refresh = issue_scoped_tokens(user, token_scope, tenant=tenant if scope == 'client' else None)
        res = Response({
            'message': 'Login successful',
            'user': _user_payload(user),
            'session_scope': token_scope,
        })
        set_auth_cookies(res, scope, access, refresh, tenant=tenant if scope == 'client' else None)
        return res


class RefreshTokenCookieView(APIView):
    """Rotate one audience's access cookie without crossing applications."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        _enforce_csrf(request)
        tenant = resolve_tenant(request)
        requested = (request.data.get('scope') or '').strip().lower()
        aliases = {'platform': 'staff', 'merchant': 'owner', 'customer': 'client'}
        requested = aliases.get(requested, requested)
        if requested and requested not in SCOPE_ORDER:
            return Response({'detail': 'Unknown session scope.'}, status=status.HTTP_400_BAD_REQUEST)

        candidates = []
        for scope in SCOPE_ORDER:
            if scope == 'client' and not tenant:
                continue
            names = refresh_cookie_names(scope, tenant if scope == 'client' else None)
            token = next((request.COOKIES[name] for name in names if request.COOKIES.get(name)), None)
            if token:
                candidates.append((scope, token))
        if requested:
            candidates = [candidate for candidate in candidates if candidate[0] == requested]
        if not candidates:
            return Response({'detail': 'Refresh token missing.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(candidates) > 1:
            return Response(
                {'detail': 'Choose the session scope to refresh.'},
                status=status.HTTP_409_CONFLICT,
            )
        found_scope, refresh_str = candidates[0]
        try:
            token = RefreshToken(refresh_str)
            expected_scope = {'staff': PLATFORM_SCOPE, 'owner': MERCHANT_SCOPE, 'client': CUSTOMER_SCOPE}[found_scope]
            if token.get('auth_scope') != expected_scope:
                raise TokenError('Wrong token audience.')
            if found_scope == 'client' and token.get('tenant') != tenant:
                raise TokenError('Wrong customer tenant.')
            user = get_user_model().objects.get(pk=token['user_id'], is_active=True)
            if found_scope == 'staff' and not (user.is_staff or user.is_superuser):
                raise TokenError('Staff access revoked.')
            if found_scope == 'owner' and not has_merchant_access(user):
                raise TokenError('Merchant access revoked.')
            if found_scope == 'client' and not _is_store_customer(user, tenant):
                raise TokenError('Customer access revoked.')
            if not compare_digest(str(token.get('password_state', '')), user.get_session_auth_hash()):
                raise TokenError('Session revoked after password change.')
            access_token = str(token.access_token)
        except (TokenError, get_user_model().DoesNotExist):
            return Response({'detail': 'Invalid or expired refresh token.'}, status=status.HTTP_401_UNAUTHORIZED)

        response = Response({'message': 'Token refreshed.', 'scope': found_scope})
        set_auth_cookies(
            response, found_scope, access_token, refresh_str,
            tenant=tenant if found_scope == 'client' else None,
        )
        return response


class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        _enforce_csrf(request)
        res = Response({'message': 'Logged out.'})
        clear_backoffice_cookies(res)
        tenant = resolve_tenant(request)
        if tenant:
            clear_client_cookies(res, tenant)
        return res


class CheckAuthView(APIView):
    """Used by the showroom to restore session on mount."""
    authentication_classes = [OwnerCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'message': 'Authenticated', 'user': _user_payload(request.user)})


def _is_store_customer(user, tenant):
    if not tenant:
        return False
    from store.models import StoreCustomer

    return StoreCustomer.objects.filter(profile__user=user, store__slug=tenant).exists()
