from django.urls import path

from .views import (
    CSRFTokenView,
    MerchantPasswordResetRequestView,
    MerchantPasswordResetConfirmView,
    CheckAuthView,
    CustomTokenObtainPairView,
    LogoutView,
    RefreshTokenCookieView,
    RegisterView,
    ResendMerchantVerificationView,
    VerifyMerchantEmailView,
)

urlpatterns = [
    path('csrf', CSRFTokenView.as_view()),
    path('register', RegisterView.as_view()),
    path('verify-email', VerifyMerchantEmailView.as_view(), name='merchant-email-verify'),
    path('verify-email/resend', ResendMerchantVerificationView.as_view()),
    path('password-reset', MerchantPasswordResetRequestView.as_view()),
    path('password-reset/confirm', MerchantPasswordResetConfirmView.as_view()),
    path('token', CustomTokenObtainPairView.as_view()),
    path('token/refresh', RefreshTokenCookieView.as_view()),
    path('logout', LogoutView.as_view()),
    path('me', CheckAuthView.as_view()),
]
