"""PostgreSQL-only settings for transaction and row-lock correctness tests.

The primary database credentials are inherited from ``base``. Django's test
runner is forced to create/use a separately named database whose name starts
with ``test_``; importing this module fails closed if that isolation guarantee
is not satisfied.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False
SECRET_KEY = "django-insecure-roomloop-isolated-postgresql-test-key-2026"
ALLOWED_HOSTS = ["*"]

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

database = DATABASES["default"]
if database.get("ENGINE") != "django.db.backends.postgresql":
    raise ImproperlyConfigured(
        "PostgreSQL correctness tests require django.db.backends.postgresql."
    )

primary_database_name = str(database.get("NAME") or "")
isolated_test_database_name = os.getenv(
    "ROOMLOOP_POSTGRES_TEST_DB",
    "test_roomloop_concurrency",
).strip()

if (
    not isolated_test_database_name.lower().startswith("test_")
    or isolated_test_database_name == primary_database_name
):
    raise ImproperlyConfigured(
        "ROOMLOOP_POSTGRES_TEST_DB must start with 'test_' and must not equal "
        "the application database name."
    )

database["CONN_MAX_AGE"] = 0
database.setdefault("TEST", {})["NAME"] = isolated_test_database_name
