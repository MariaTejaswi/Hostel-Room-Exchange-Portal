"""Database configuration helpers shared by RoomLoop settings modules."""

import re
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


# Query parameters accepted by libpq/psycopg. Unknown URL parameters are
# intentionally ignored instead of being forwarded as invalid connect kwargs.
_LIBPQ_QUERY_OPTIONS = {
    "application_name",
    "channel_binding",
    "connect_timeout",
    "gssencmode",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "options",
    "passfile",
    "service",
    "sslcert",
    "sslcrl",
    "sslkey",
    "sslmode",
    "sslrootcert",
    "target_session_attrs",
    "tcp_user_timeout",
}
_VALID_SSL_MODES = {
    "allow",
    "disable",
    "prefer",
    "require",
    "verify-ca",
    "verify-full",
}
_PUBLIC_SEARCH_PATH = re.compile(
    r"(?:^|\s)-c\s*search_path\s*=\s*public(?:\s|$)",
    re.IGNORECASE,
)


def _last_nonempty(query, key):
    return next((value for value in reversed(query.get(key, [])) if value), None)


def _with_public_search_path(options):
    options = (options or "").strip()
    if _PUBLIC_SEARCH_PATH.search(options):
        return options
    return f"{options} -c search_path=public".strip()


def parse_postgresql_database_url(database_url, *, conn_max_age=60):
    """Convert a PostgreSQL URL into a Django database configuration.

    Neon requires TLS and RoomLoop stores its objects in the ``public`` schema.
    Compatible libpq query parameters are retained, while unknown parameters
    are ignored so they cannot become invalid psycopg keyword arguments.
    """

    try:
        parsed = urlparse(database_url)
        port = parsed.port or 5432
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("DATABASE_URL contains an invalid PostgreSQL port.") from exc

    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use the postgres or postgresql scheme.")
    if not parsed.hostname:
        raise ImproperlyConfigured("DATABASE_URL must include a PostgreSQL hostname.")

    database_name = unquote(parsed.path.lstrip("/"))
    if not database_name:
        raise ImproperlyConfigured("DATABASE_URL must include a PostgreSQL database name.")

    query = parse_qs(parsed.query, keep_blank_values=False)
    connection_options = {}
    for key in _LIBPQ_QUERY_OPTIONS:
        value = _last_nonempty(query, key)
        if value is not None:
            connection_options[key] = value

    is_neon = parsed.hostname.endswith(".neon.tech")
    default_sslmode = "require" if is_neon else "prefer"
    sslmode = connection_options.get("sslmode", default_sslmode).lower()
    if sslmode not in _VALID_SSL_MODES or (
        is_neon and sslmode not in {"require", "verify-ca", "verify-full"}
    ):
        sslmode = default_sslmode
    connection_options["sslmode"] = sslmode
    connection_options["options"] = _with_public_search_path(
        connection_options.get("options")
    )

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": database_name,
        "USER": unquote(parsed.username) if parsed.username else "postgres",
        "PASSWORD": unquote(parsed.password) if parsed.password else "",
        "HOST": parsed.hostname,
        "PORT": port,
        "CONN_MAX_AGE": conn_max_age,
        "OPTIONS": connection_options,
        "TEST": {
            "CHARSET": None,
            "COLLATION": None,
            "MIGRATE": True,
            "MIRROR": None,
            "NAME": None,
        },
    }
