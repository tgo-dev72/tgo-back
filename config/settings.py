"""
Django settings for the store-platform SaaS project.

Serves two audiences with separate identity:
  - platform merchants (own stores) — accounts app
  - the showroom (design gallery / store deployment portal)
Deployed storefronts themselves are scaffolded/deployed by storefront_factory
and are served by the future multi-tenant `store` API.
"""

from datetime import timedelta
from pathlib import Path
import os
import re
import sys
from urllib.parse import urlparse, parse_qsl

from dotenv import load_dotenv
from corsheaders.defaults import default_headers
import cloudinary

from .origins import LOCAL_DEVELOPMENT_ORIGIN_REGEX

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
running_tests = len(sys.argv) > 1 and sys.argv[1] == "test"

# The design-stock monorepo (unconfigured storefront designs) that
# storefront_factory scaffolds new stores from.
DESIGN_STOCK_PATH = os.environ.get(
    'DESIGN_STOCK_PATH',
    str(BASE_DIR.parent / 'design-stock'),
)
if running_tests:
    # Tests must not depend on a sibling checkout or network access. Individual
    # pinned-source tests can still override these settings explicitly.
    DESIGN_STOCK_PATH = os.environ.get(
        "TEST_DESIGN_STOCK_PATH",
        str(BASE_DIR / "tests" / "fixtures" / "design-stock"),
    )

# GitHub repo holding design-stock (its manifest + design templates).
# Used for source/catalog reads and as the legacy generated-store destination.
# Do not change this to the deployment repo: use STOREFRONT_DEPLOYMENT_REPO.
DESIGN_STOCK_REPO = os.environ.get('DESIGN_STOCK_REPO', 'EnnacherZ/design-stock')
# Immutable 40-character design-stock commit approved for storefront creation
# and updates. Pinning this prevents unrelated generated-app commits on main
# from making every deployed store appear outdated. Empty keeps the current
# main revision for local development only.
STOREFRONT_RELEASE_REVISION = os.environ.get('STOREFRONT_RELEASE_REVISION', '').strip().lower()
if running_tests:
    STOREFRONT_RELEASE_REVISION = ""

# GitHub token with Contents write access to DESIGN_STOCK_REPO. Creation and
# redesign publish apps/<slug>; confirmed store deletion removes that path.
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
# Source reads may use a read-only credential. GITHUB_TOKEN remains the legacy
# writer until stores in DESIGN_STOCK_REPO have been migrated.
STOREFRONT_SOURCE_TOKEN = os.environ.get('STOREFRONT_SOURCE_TOKEN', '')
# Empty preserves the legacy destination. Only newly pinned requests use this
# destination; persisted requests never follow a later environment switch.
STOREFRONT_DEPLOYMENT_REPO = os.environ.get('STOREFRONT_DEPLOYMENT_REPO', '').strip()
STOREFRONT_DEPLOYMENT_BRANCH = os.environ.get('STOREFRONT_DEPLOYMENT_BRANCH', 'main').strip()
STOREFRONT_DEPLOYMENT_TOKEN = os.environ.get('STOREFRONT_DEPLOYMENT_TOKEN', '')
# Rollback switch for NEW requests only. Retain target coordinates/credential
# so already-pinned new-repository stores can still update, retry and delete.
STOREFRONT_NEW_REPO_ENABLED = os.environ.get('STOREFRONT_NEW_REPO_ENABLED', 'true').lower() in ('true', '1', 'yes')
VERCEL_API_TOKEN = os.environ.get('VERCEL_API_TOKEN', '')
VERCEL_TEAM_ID = os.environ.get('VERCEL_TEAM_ID', '')
STOREFRONT_MIGRATION_PREVIEW_BYPASS_SECRET = os.environ.get('STOREFRONT_MIGRATION_PREVIEW_BYPASS_SECRET', '')
# Public URL of this backend, used as VITE_API_URL for every scaffolded
# storefront's Vercel deployment.
STOREFRONT_API_URL = os.environ.get('STOREFRONT_API_URL', 'https://api.tijarago.com').rstrip('/')
# Local dev backend base used in scaffolded apps' `.env.development`, so
# `npm run dev` on a new storefront renders the real API (LAN/laptop host).
DEV_API_URL = os.environ.get('DEV_API_URL', 'https://127.0.0.1:8001')
# Demo API used by design previews (the mock-api server). Previews are built
# in "preview" mode so they simulate with sample data; deployed builds switch
# to STOREFRONT_API_URL. Both run over HTTPS to avoid mixed-content blocks.
DEMO_API_URL = os.environ.get('DEMO_API_URL', 'https://127.0.0.1:8090')
# Base domain new storefronts get a subdomain under (e.g. <slug>.tijarago.com).
STOREFRONT_BASE_DOMAIN = os.environ.get('STOREFRONT_BASE_DOMAIN', 'tijarago.com')
STOREFRONT_INDEXING_ENABLED = os.environ.get('STOREFRONT_INDEXING_ENABLED', 'False') == 'True'
THEME_PREVIEW_SIGNUP_URL = os.environ.get(
    "THEME_PREVIEW_SIGNUP_URL", "https://www.tijarago.com/signup",
).strip()
THEME_PREVIEW_ALLOWED_ORIGINS = tuple(
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "THEME_PREVIEW_ALLOWED_ORIGINS", "https://www.tijarago.com",
    ).split(",")
    if origin.strip().startswith("https://")
)
# Opt-in provider address for new stores; existing recorded domains stay put.
# Explicitly allow each public storefront origin in CORS/CSRF when using this.
STOREFRONT_USE_VERCEL_DOMAIN = os.environ.get('STOREFRONT_USE_VERCEL_DOMAIN', 'false').lower() in ('true', '1', 'yes')
# Cloudflare API token scoped to DNS:Edit on that zone — used to create the
# <slug>.<base domain> CNAME record automatically at deploy time.
CLOUDFLARE_API_TOKEN = os.environ.get('CLOUDFLARE_API_TOKEN', '')
CLOUDFLARE_ZONE_ID = os.environ.get('CLOUDFLARE_ZONE_ID', '')
# Separate least-privilege token for merchant email-routing API operations.
CLOUDFLARE_EMAIL_ROUTING_TOKEN = os.environ.get('CLOUDFLARE_EMAIL_ROUTING_TOKEN', '')
CLOUDFLARE_ACCOUNT_ID = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
STORE_EMAIL_DOMAIN = os.environ.get('STORE_EMAIL_DOMAIN', 'mail.tijarago.com').strip().lower()
CLOUDFLARE_EMAIL_WORKER_NAME = os.environ.get("CLOUDFLARE_EMAIL_WORKER_NAME", "").strip()
MAILBOX_TRANSPORT_MODE = os.environ.get("MAILBOX_TRANSPORT_MODE", "cloud").strip().lower()
STOREFRONT_HEALTHCHECK_TIMEOUT = max(
    1,
    int(os.environ.get("STOREFRONT_HEALTHCHECK_TIMEOUT", "15")),
)
ANALYTICS_EVENT_RETENTION_DAYS = max(
    30,
    int(os.environ.get("ANALYTICS_EVENT_RETENTION_DAYS", "395")),
)
CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()
VERCEL_API_TIMEOUT = max(5, int(os.environ.get("VERCEL_API_TIMEOUT", "30")))
VERCEL_API_READ_ATTEMPTS = max(1, min(5, int(os.environ.get("VERCEL_API_READ_ATTEMPTS", "3"))))

# Payment gateways are adapters behind a stable business interface. Switching
# the active platform gateway changes this setting and adds/configures one
# adapter; subscription and storefront business services remain unchanged.
PLATFORM_PAYMENT_GATEWAY = os.environ.get(
    "PLATFORM_PAYMENT_GATEWAY", "youcanpay",
).strip().lower()
STOREFRONT_DEFAULT_PAYMENT_GATEWAY = os.environ.get(
    "STOREFRONT_DEFAULT_PAYMENT_GATEWAY", "youcanpay",
).strip().lower()

# Merchant-owned online gateways that are intentionally advertised but cannot
# yet be selected. Keep their runtime adapters (when present) available for
# historical transactions and platform billing.
STOREFRONT_FUTURE_PAYMENT_PROVIDERS = {
    "paypal": "PayPal",
    "youcanpay": "YouCan Pay",
    "stripe": "Stripe",
}

# Platform-owned gateway credentials for merchant SaaS subscriptions. Names
# remain provider-neutral; the active adapter interprets their values.
PLATFORM_PAYMENT_PRIVATE_KEY = os.environ.get('PLATFORM_PAYMENT_PRIVATE_KEY', '').strip()
PLATFORM_PAYMENT_PUBLIC_KEY = os.environ.get('PLATFORM_PAYMENT_PUBLIC_KEY', '').strip()
PLATFORM_PAYMENT_ACCESS_TOKEN = os.environ.get('PLATFORM_PAYMENT_ACCESS_TOKEN', '').strip()
PLATFORM_PAYMENT_SANDBOX = os.environ.get(
    'PLATFORM_PAYMENT_SANDBOX', 'false'
).strip().lower() in {'1', 'true', 'yes', 'on'}
PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION = os.environ.get(
    'PLATFORM_PAYMENT_REQUIRE_TRANSACTION_VERIFICATION',
    'false' if PLATFORM_PAYMENT_SANDBOX else 'true',
).strip().lower() in {'1', 'true', 'yes', 'on'}
PLATFORM_PAYMENT_API_TIMEOUT = max(
    1,
    int(os.environ.get("PLATFORM_PAYMENT_API_TIMEOUT", "10")),
)

# PayPal Sandbox credentials keep the original variable names for backwards
# compatibility. Live credentials are deliberately separate so an admin mode
# switch can never send sandbox secrets to PayPal's production API.
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
PAYPAL_RETURN_URL = os.environ.get("PAYPAL_RETURN_URL", "").strip()
PAYPAL_CANCEL_URL = os.environ.get("PAYPAL_CANCEL_URL", "").strip()
PAYPAL_MERCHANT_ID = os.environ.get("PAYPAL_MERCHANT_ID", "").strip()
PAYPAL_RECEIVER_EMAIL = os.environ.get("PAYPAL_RECEIVER_EMAIL", "").strip()
PAYPAL_LIVE_CLIENT_ID = os.environ.get("PAYPAL_LIVE_CLIENT_ID", "").strip()
PAYPAL_LIVE_CLIENT_SECRET = os.environ.get("PAYPAL_LIVE_CLIENT_SECRET", "").strip()
PAYPAL_LIVE_WEBHOOK_ID = os.environ.get("PAYPAL_LIVE_WEBHOOK_ID", "").strip()
PAYPAL_LIVE_MERCHANT_ID = os.environ.get("PAYPAL_LIVE_MERCHANT_ID", "").strip()
PAYPAL_LIVE_RECEIVER_EMAIL = os.environ.get("PAYPAL_LIVE_RECEIVER_EMAIL", "").strip()
PAYPAL_API_TIMEOUT = max(1, int(os.environ.get("PAYPAL_API_TIMEOUT", "10")))
SUBSCRIPTION_CHECKOUT_INITIALIZATION_TIMEOUT_SECONDS = max(
    15,
    int(os.environ.get("SUBSCRIPTION_CHECKOUT_INITIALIZATION_TIMEOUT_SECONDS", "120")),
)
SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS = max(
    300,
    int(os.environ.get("SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS", "900")),
)
MERCHANT_DASHBOARD_URL = os.environ.get(
    'MERCHANT_DASHBOARD_URL', 'https://dash.tijarago.com'
).rstrip('/')
MERCHANT_TERMS_VERSION = os.environ.get('MERCHANT_TERMS_VERSION', '2026-08').strip()
PHONENUMBER_DB_FORMAT = 'E164'

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-only-change-me')
SECRET_KEY_FALLBACKS = [
    key.strip()
    for key in os.environ.get("DJANGO_SECRET_KEY_FALLBACKS", "").split(",")
    if key.strip() and key.strip() != SECRET_KEY
]
DEBUG = os.environ.get('IS_DEBUG') == "True"
STOREFRONT_DEPLOYMENT_MODE = os.environ.get('STOREFRONT_DEPLOYMENT_MODE', 'cloud').strip().lower()
if STOREFRONT_DEPLOYMENT_MODE not in {'cloud', 'local'}:
    raise ValueError('STOREFRONT_DEPLOYMENT_MODE must be cloud or local.')
if STOREFRONT_DEPLOYMENT_MODE == 'local' and (not DEBUG or os.environ.get('VERCEL') == '1'):
    raise ValueError('Local storefront deployment requires IS_DEBUG=True outside Vercel.')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'django_extensions',
    'phonenumber_field',
    'cloudinary',
    'cloudinary_storage',
    'accounts',
    'store',
    'access_control',
    'catalog',
    'coupons',
    'orders',
    'payments',
    'marketing',
    'notifications',
    'store_mail',
    'api',
    'storefront_factory',
    'platform_admin',
    'subscriptions',
    'commerce_analytics',
    'theme_previewer',
]

MIDDLEWARE = [
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'config.middleware.StorefrontOriginBindingMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'config.middleware.PlatformCsrfViewMiddleware',
    'theme_previewer.middleware.DemoStoreMutationGuardMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, "templates")],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database — SQLite is the zero-configuration local default. Set DATABASE_URL
# to a PostgreSQL URL for shared/staging/production environments. Django's
# test command deliberately ignores DATABASE_URL and uses an isolated in-memory
# database unless TEST_DATABASE_URL is explicitly supplied. This prevents a
# developer test run from creating/dropping databases through cloud credentials.
database_url = (
    os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    if running_tests
    else os.getenv("DATABASE_URL", "")
).strip()
if database_url.startswith(("sqlite://", "sqlite3://")):
    parsed_database_url = urlparse(database_url)
    sqlite_name = parsed_database_url.path or parsed_database_url.netloc
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:" if sqlite_name in ("", "/:memory:") else sqlite_name,
        }
    }
elif database_url:
    parsed_database_url = urlparse(database_url)
    if parsed_database_url.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use postgresql:// or sqlite://")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": parsed_database_url.path.lstrip("/"),
            "USER": parsed_database_url.username,
            "PASSWORD": parsed_database_url.password,
            "HOST": parsed_database_url.hostname,
            "PORT": parsed_database_url.port or 5432,
            "OPTIONS": dict(parse_qsl(parsed_database_url.query)),
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
    {'NAME': 'accounts.password_validation.LetterAndNumberPasswordValidator'},
]

LANGUAGE_CODE = 'ar'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
# Vercel does not run collectstatic for this project. WhiteNoise therefore
# resolves Django admin/app assets through finders instead of a missing
# production manifest.
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'accounts.AuthUser'

# Media — Cloudinary when configured, otherwise real local file storage
# (used for seeded product images so storefronts render real images locally).
MEDIA_ROOT = os.environ.get("DJANGO_MEDIA_ROOT", os.path.join(BASE_DIR, "media"))
MEDIA_URL = '/media/'
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.environ.get("CLOUDINARY_CLOUD_NAME"),
    'API_KEY': os.environ.get("CLOUDINARY_API_KEY"),
    'API_SECRET': os.environ.get("CLOUDINARY_API_SECRET"),
    'RESOURCE_TYPE': 'image',
}

cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
    secure=True,
    access_mode='public',
)
media_storage_backend = os.environ.get(
    "DJANGO_FILE_STORAGE",
    (
        "cloudinary_storage.storage.MediaCloudinaryStorage"
        if os.environ.get("CLOUDINARY_CLOUD_NAME")
        else "django.core.files.storage.FileSystemStorage"
    ),
).strip()
if "test" in sys.argv:
    media_storage_backend = "django.core.files.storage.FileSystemStorage"

# Django 5 uses storage aliases. The removed DEFAULT_FILE_STORAGE setting was
# silently leaving production uploads on Vercel's read-only filesystem.
STORAGES = {
    "default": {"BACKEND": media_storage_backend},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'accounts.authentication.OwnerCookieJWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'merchant_registration': '10/hour',
        'email_verification': '5/hour',
        'customer_registration': '10/hour',
        'customer_login': '10/minute',
        'customer_email_verification': '5/hour',
        'merchant_password_reset': '5/hour',
        'merchant_password_reset_confirm': '10/hour',
        'customer_password_reset': '5/hour',
        'customer_password_reset_confirm': '10/hour',
        'store_creation': '20/hour',
        'store_availability': '120/minute',
        'store_email_routing': '10/hour',
        'store_mail_send': '60/hour',
        'storefront_configuration': '30/hour',
        'storefront_deployment': '20/hour',
        'product_review': '10/hour',
        'storefront_analytics_event': '300/hour',
        'storefront_contact': '8/hour',
        'storefront_domain_connect': '20/hour',
        'storefront_domain_verify': '30/hour',
        'storefront_domain_disconnect': '20/hour',
        'storefront_domain_primary': '20/hour',
    },
}

STOREFRONT_CUSTOM_DOMAINS_ENABLED = os.getenv(
    "STOREFRONT_CUSTOM_DOMAINS_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
STOREFRONT_DNS_RESOLVER_URL = os.getenv(
    "STOREFRONT_DNS_RESOLVER_URL", "https://cloudflare-dns.com/dns-query"
).strip()

# JWT — stored in httpOnly cookies, so the showroom never touches the tokens.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
    'ROTATE_REFRESH_TOKENS':  True,
    'BLACKLIST_AFTER_ROTATION': False,
    'AUTH_COOKIE':          'access_token',
    'AUTH_COOKIE_REFRESH':  'refresh_token',
    'AUTH_COOKIE_SECURE':   True,
    'AUTH_COOKIE_HTTP_ONLY': True,
    'AUTH_COOKIE_SAMESITE': 'None',
}

APPEND_SLASH = False

ALLOWED_HOSTS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_HOSTS", "").split(",")
    if origin.strip()
]
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
# Storefront dev servers claim ports 5173+ (designs and every scaffolded
# apps/<slug> gets the next free one) — allow the whole dev range so a live
# catalog call from a Vite dev server is never blocked.
CORS_ALLOWED_ORIGIN_REGEXES = ([LOCAL_DEVELOPMENT_ORIGIN_REGEX] if DEBUG else []) + [
    # Production storefronts live on <slug>.<configured base domain>.
    rf"^https://[a-z0-9-]+\.{re.escape(STOREFRONT_BASE_DOMAIN.strip().lower().lstrip('.'))}$",
]
CORS_ALLOW_CREDENTIALS = True

# Custom browser headers used by the platform clients must be advertised in
# CORS preflight responses. Header names are lower-case here because the CORS
# standard compares them case-insensitively and django-cors-headers normalizes
# its defaults the same way.
CORS_ALLOW_HEADERS = default_headers + (
    "idempotency-key",
    "x-store-slug",
    "x-preview-ticket",
    "x-preview-preset",
)

CSRF_COOKIE_NAME = "csrftoken"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
# Cross-site storefront dev servers (localhost:5173 -> this API) must receive
# the csrftoken cookie; same-site production subdomains work either way.
CSRF_COOKIE_SAMESITE = "None"
_configured_csrf_origins = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", os.getenv("CORS_ALLOWED_ORIGINS", "")).split(",")
    if origin.strip()
]
_storefront_base_domain = STOREFRONT_BASE_DOMAIN.strip().lower().lstrip(".")
_storefront_csrf_origins = (
    [f"https://*.{_storefront_base_domain}"]
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", _storefront_base_domain)
    else []
)
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([
    *_configured_csrf_origins,
    *_storefront_csrf_origins,
]))

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.smtp.EmailBackend",
).strip()
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "on"}
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ValueError("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.")
EMAIL_TIMEOUT = max(1, int(os.environ.get("EMAIL_TIMEOUT", "10")))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER or "").strip()
TEST_MAIL = os.environ.get("TEST_MAIL", "").strip()
MAILBOX_INBOUND_SECRET = os.environ.get("MAILBOX_INBOUND_SECRET", "").strip()
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
MAILBOX_RETENTION_DAYS = max(30, int(os.environ.get("MAILBOX_RETENTION_DAYS", "90")))
EMAIL_VERIFICATION_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("EMAIL_VERIFICATION_TIMEOUT_SECONDS", "600")),
)
EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = max(
    0,
    int(os.environ.get("EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS", "60")),
)
PASSWORD_RESET_TIMEOUT_SECONDS = max(300, min(3600, int(os.environ.get("PASSWORD_RESET_TIMEOUT_SECONDS", "1800"))))

# Public TijaraGO identity used by platform-owned transactional emails.
SHOWROOM_URL = os.environ.get(
    "SHOWROOM_URL", "https://www.tijarago.com"
).rstrip("/")
PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "TijaraGO").strip() or "TijaraGO"
PLATFORM_TAGLINE = os.environ.get("PLATFORM_TAGLINE", "Commerce, made simple").strip()
PLATFORM_BRAND_COLOR = os.environ.get("PLATFORM_BRAND_COLOR", "#1F8A8C").strip()
PLATFORM_BRAND_ACCENT_COLOR = os.environ.get("PLATFORM_BRAND_ACCENT_COLOR", "#20C6C8").strip()
PLATFORM_BRAND_INK_COLOR = os.environ.get("PLATFORM_BRAND_INK_COLOR", "#073B3C").strip()
PLATFORM_BRAND_GOLD_COLOR = os.environ.get("PLATFORM_BRAND_GOLD_COLOR", "#D5B36A").strip()
PLATFORM_EMAIL_LOGO_URL = os.environ.get(
    "PLATFORM_EMAIL_LOGO_URL",
    f"{SHOWROOM_URL}/brand/logos-4x/tyjara-go-teal-cyan-4x.png",
).strip()
SHOWROOM_EMAIL_BRAND_COLOR = os.environ.get("SHOWROOM_EMAIL_BRAND_COLOR", "#0E92E4").strip()
SHOWROOM_EMAIL_BRAND_ACCENT_COLOR = os.environ.get("SHOWROOM_EMAIL_BRAND_ACCENT_COLOR", "#00B4D8").strip()
SHOWROOM_EMAIL_BRAND_INK_COLOR = os.environ.get("SHOWROOM_EMAIL_BRAND_INK_COLOR", "#073B5C").strip()
SHOWROOM_EMAIL_LOGO_URL = os.environ.get(
    "SHOWROOM_EMAIL_LOGO_URL",
    f"{SHOWROOM_URL}/brand/logos-4x/tyjara-go-blue-cyan-4x.png",
).strip()
MERCHANT_EMAIL_BRAND_COLOR = os.environ.get("MERCHANT_EMAIL_BRAND_COLOR", "#2D6A4F").strip()
MERCHANT_EMAIL_BRAND_ACCENT_COLOR = os.environ.get("MERCHANT_EMAIL_BRAND_ACCENT_COLOR", "#52B788").strip()
MERCHANT_EMAIL_BRAND_INK_COLOR = os.environ.get("MERCHANT_EMAIL_BRAND_INK_COLOR", "#1B4332").strip()
MERCHANT_EMAIL_LOGO_URL = os.environ.get(
    "MERCHANT_EMAIL_LOGO_URL",
    f"{MERCHANT_DASHBOARD_URL}/brand/logos-4x/tyjara-go-green-lime-4x.png",
).strip()
PLATFORM_WEBSITE_URL = os.environ.get(
    "PLATFORM_WEBSITE_URL", SHOWROOM_URL
).rstrip("/")
PLATFORM_SUPPORT_EMAIL = os.environ.get(
    "PLATFORM_SUPPORT_EMAIL", EMAIL_HOST_USER or ""
).strip()
EMAIL_VERIFICATION_FRONTEND_URL = os.environ.get(
    "EMAIL_VERIFICATION_FRONTEND_URL",
    f"{MERCHANT_DASHBOARD_URL}/verify-email",
).strip()
# Public base used to build unsubscribe links in marketing emails.
UNSUBSCRIBE_BASE_URL = os.environ.get('UNSUBSCRIBE_BASE_URL', '')

# Production hardening — only once a Postgres-backed (deployed) environment.
# Vercel terminates TLS and forwards over HTTP internally.
if not DEBUG and database_url and not database_url.startswith(("sqlite://", "sqlite3://")):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Start with a short, reversible HSTS window. Increase it deliberately
    # after the production domain and every required subdomain are HTTPS-only.
    SECURE_HSTS_SECONDS = max(0, int(os.environ.get("SECURE_HSTS_SECONDS", "3600")))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = os.environ.get(
        "SECURE_HSTS_INCLUDE_SUBDOMAINS", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    SECURE_HSTS_PRELOAD = os.environ.get(
        "SECURE_HSTS_PRELOAD", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
