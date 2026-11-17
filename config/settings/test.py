import os
from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "django-insecure-roomloop-isolated-test-key-2026"
ALLOWED_HOSTS = ["*"]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Guarantee memory-only email backend during tests
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Isolated test database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_roomloop.sqlite3",
    }
}
