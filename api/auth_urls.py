from django.urls import path

from .auth import (
    CustomerCSRFView,
    CustomerProfileView,
    CustomerPasswordResetRequestView,
    CustomerPasswordResetConfirmView,
    LoginView,
    LogoutView,
    MeView,
    RegisterView,
    ResendCustomerVerificationView,
    VerifyCustomerEmailView,
)

urlpatterns = [
    path('csrf/', CustomerCSRFView.as_view()),
    path('register/', RegisterView.as_view()),
    path('verify-email/', VerifyCustomerEmailView.as_view()),
    path('resend-verification/', ResendCustomerVerificationView.as_view()),
    path('login/', LoginView.as_view()),
    path('password-reset/', CustomerPasswordResetRequestView.as_view()),
    path('password-reset/confirm/', CustomerPasswordResetConfirmView.as_view()),
    path('logout/', LogoutView.as_view()),
    path('me/', MeView.as_view()),
    path('profile/', CustomerProfileView.as_view()),
]
