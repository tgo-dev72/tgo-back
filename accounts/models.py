from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils.translation import gettext_lazy as _
from phonenumber_field.modelfields import PhoneNumberField


class AuthUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class AuthUser(AbstractUser):
    """Identity + credentials only.

    Domain roles are deliberately NOT stored here:
      - who a user is *within a store*  → store.StoreMember
      - customer identity                → store.ClientProfile
      - customer access within a store   → store.StoreCustomer
      - platform staff (your team)      → dedicated Django is_staff identity

    A person can be a merchant (owner of store A), a team member (store B)
    and a customer (of store C) at the same time. Platform operators use a
    separate work identity so internal authority cannot leak into those
    business contexts. The staff-management API enforces that boundary.
    """
    username = None
    email = models.EmailField(_("email address"), unique=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    email_verification_sent_at = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = AuthUserManager()

    def __str__(self):
        return self.email


class MerchantProfile(models.Model):
    """Marks an account that may own/create stores.

    Store team access remains on ``store.StoreMember``. Keeping this small
    onboarding principal separate prevents a customer or invited team member
    from silently becoming a store owner merely by possessing an AuthUser.
    """

    class PreferredLanguage(models.TextChoices):
        ENGLISH = "en", "English"
        FRENCH = "fr", "French"
        ARABIC = "ar", "Arabic"

    user = models.OneToOneField(
        AuthUser,
        on_delete=models.CASCADE,
        related_name="merchant_profile",
    )
    business_name = models.CharField(max_length=150, blank=True, default="")
    phone_number = PhoneNumberField(
        blank=True,
        default="",
    )
    phone_verified_at = models.DateTimeField(null=True, blank=True)
    preferred_language = models.CharField(
        max_length=2,
        choices=PreferredLanguage.choices,
        default=PreferredLanguage.ARABIC,
    )
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_phone_verified(self):
        return self.phone_verified_at is not None

    def __str__(self):
        return f"{self.user.email} (merchant account)"


class PasswordResetToken(models.Model):
    """One-use hashed credential recovery token. No raw token is persisted."""

    user = models.ForeignKey(AuthUser, on_delete=models.CASCADE, related_name="password_reset_tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    scope = models.CharField(max_length=16, choices=[("merchant", "Merchant"), ("customer", "Customer")])
    store = models.ForeignKey("store.Store", null=True, blank=True, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "scope", "created_at"])]
