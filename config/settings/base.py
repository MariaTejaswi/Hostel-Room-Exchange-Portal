import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

# Base directory: points to repository root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Load .env file
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-roomloop-default-key-replace-in-prod")

DEBUG = False

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",")
    if host.strip()
]

# Application definition
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = []

LOCAL_APPS = [
    "apps.core.apps.CoreConfig",
    "apps.accounts.apps.AccountsConfig",
    "apps.hostels.apps.HostelsConfig",
    "apps.exchange.apps.ExchangeConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.navigation_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Custom User Model
AUTH_USER_MODEL = "accounts.User"

# Database Configuration (PostgreSQL 16)
# Preference logic:
# 1. Local development prefers individual DB_* variables so that an outdated or default
#    DATABASE_URL in .env does not silently override working local credentials.
# 2. In Docker, production, or PaaS environments where DATABASE_URL is standard (and DB_PASSWORD
#    is unset or PREFER_DATABASE_URL=True), DATABASE_URL is parsed with URL unquoting.
# Insecure hardcoded password fallbacks have been removed.
database_url = os.getenv("DATABASE_URL")
db_password = os.getenv("DB_PASSWORD")
prefer_db_url = os.getenv("PREFER_DATABASE_URL", "False").lower() in ("true", "1")

if database_url and (prefer_db_url or db_password is None):
    parsed = urlparse(database_url)
    DATABASES = {
        "default": {
            "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.postgresql"),
            "NAME": unquote(parsed.path.lstrip("/")) if parsed.path else os.getenv("DB_NAME", "roomloop_db"),
            "USER": unquote(parsed.username) if parsed.username else os.getenv("DB_USER", "postgres"),
            "PASSWORD": unquote(parsed.password) if parsed.password else "",
            "HOST": parsed.hostname or os.getenv("DB_HOST", "localhost"),
            "PORT": parsed.port or int(os.getenv("DB_PORT", 5432)),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", 60)),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": os.getenv("DB_ENGINE", "django.db.backends.postgresql"),
            "NAME": os.getenv("DB_NAME", "roomloop_db"),
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": int(os.getenv("DB_PORT", 5432)),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", 60)),
        }
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_MAX_AGE = 31536000

# Cache configuration (In-memory default with Redis fallback support)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "roomloop-default-cache",
    }
}

# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication URLs
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Session settings
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = int(os.getenv("SESSION_COOKIE_AGE", 86400))
SESSION_EXPIRE_AT_BROWSER_CLOSE = os.getenv("SESSION_EXPIRE_AT_BROWSER_CLOSE", "False").lower() in ("true", "1")
SESSION_COOKIE_SAMESITE = "Lax"

# CSRF settings
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
csrf_origins_env = os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in csrf_origins_env.split(",")
    if origin.strip()
] or [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost",
    "http://127.0.0.1",
]

# Celery Configuration
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False
CELERY_BROKER_CONNECTION_MAX_RETRIES = 1
CELERY_BROKER_CONNECTION_TIMEOUT = 1.0
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BEAT_SCHEDULE = {
    "expire-exchange-requests-every-hour": {
        "task": "apps.exchange.tasks.expire_exchange_requests_task",
        "schedule": 3600.0,  # Every hour
    },
    "expire-exchange-proposals-every-hour": {
        "task": "apps.exchange.tasks.expire_exchange_proposals_task",
        "schedule": 3600.0,  # Every hour
    },
    "reconcile-exchange-matching-every-15-mins": {
        "task": "apps.exchange.tasks.reconcile_all_institutions_matching_task",
        "schedule": 900.0,  # Every 15 minutes
    },
}

# Email Backend
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "RoomLoop Portal <notifications@roomloop.edu>")

# Demo Accounts Visibility Shortcut
SHOW_DEMO_ACCOUNTS = os.getenv("SHOW_DEMO_ACCOUNTS", "True").lower() in ("true", "1", "yes")
