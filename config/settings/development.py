import os
from .base import *  # noqa: F403

DEBUG = True

SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-roomloop-dev-secret-key-change-in-production-2026",
)

ALLOWED_HOSTS = ["*"]

# Optional fallback flag for local testing when external PostgreSQL daemon is unavailable
if os.getenv("DJANGO_ALLOW_SQLITE_FALLBACK", "0") in ("1", "true", "True"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
        }
    }

# Email to console in development
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
