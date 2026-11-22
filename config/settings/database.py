"""Database configuration helpers shared by RoomLoop settings modules."""

import re
import shlex
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


# Query parameters accepted by libpq/psycopg. Unknown parameters fail closed
# instead of being silently discarded or forwarded as invalid connect kwargs.
_LIBPQ_QUERY_OPTIONS = {
    "application_name",
    "channel_binding",
    "client_encoding",
    "connect_timeout",
    "fallback_application_name",
    "gssencmode",
    "gsslib",
    "keepalives",
    "keepalives_count",
    "keepalives_idle",
    "keepalives_interval",
    "krbsrvname",
    "load_balance_hosts",
    "options",
    "passfile",
    "replication",
    "requirepeer",
    "requiressl",
    "service",
    "sslcert",
    "sslcrl",
    "sslcrldir",
    "sslcompression",
    "sslkey",
    "ssl_max_protocol_version",
    "ssl_min_protocol_version",
    "sslmode",
    "sslpassword",
    "sslrootcert",
    "sslsni",
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
_SEARCH_PATH_SETTING = re.compile(r"^search_path\s*=", re.IGNORECASE)


def _last_nonempty(query, key):
    return next((value for value in reversed(query.get(key, [])) if value), None)


def _without_search_path(options):
    """Remove search_path settings unsupported by Neon's pooled endpoint."""

    try:
        tokens = shlex.split(options or "", posix=True)
    except ValueError as exc:
        raise ImproperlyConfigured("DATABASE_URL contains malformed PostgreSQL options.") from exc

    filtered = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-c" and index + 1 < len(tokens):
            setting = tokens[index + 1]
            if _SEARCH_PATH_SETTING.match(setting.strip()):
                index += 2
                continue
        elif token.startswith("-c") and _SEARCH_PATH_SETTING.match(token[2:].strip()):
            index += 1
            continue

        filtered.append(token)
        index += 1

    return shlex.join(filtered)


def parse_postgresql_database_url(database_url, *, conn_max_age=60):
    """Convert a PostgreSQL URL into a Django database configuration.

    Neon requires TLS. Compatible libpq query parameters are retained, while
    unknown parameters fail with a configuration error. Neon's pooled endpoint
    rejects ``search_path`` as a startup option, so that setting is removed for
    pooled hosts and must instead be configured on the PostgreSQL role/database.
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
    unsupported_parameters = sorted(set(query) - _LIBPQ_QUERY_OPTIONS)
    if unsupported_parameters:
        names = ", ".join(unsupported_parameters)
        raise ImproperlyConfigured(
            f"DATABASE_URL contains unsupported PostgreSQL parameter(s): {names}."
        )

    connection_options = {}
    for key in _LIBPQ_QUERY_OPTIONS:
        value = _last_nonempty(query, key)
        if value is not None:
            connection_options[key] = value

    if "options" in connection_options:
        try:
            shlex.split(connection_options["options"], posix=True)
        except ValueError as exc:
            raise ImproperlyConfigured("DATABASE_URL contains malformed PostgreSQL options.") from exc

    is_neon = parsed.hostname.endswith(".neon.tech") or ".neon.tech" in parsed.hostname
    default_sslmode = "require" if is_neon else "prefer"
    sslmode = connection_options.get("sslmode", default_sslmode).lower()
    if sslmode not in _VALID_SSL_MODES or (
        is_neon and sslmode not in {"require", "verify-ca", "verify-full"}
    ):
        sslmode = default_sslmode
    connection_options["sslmode"] = sslmode

    is_neon_pooler = is_neon and ("-pooler." in parsed.hostname or "-pooler" in parsed.hostname)
    if is_neon_pooler and "options" in connection_options:
        safe_options = _without_search_path(connection_options["options"])
        if safe_options:
            connection_options["options"] = safe_options
        else:
            connection_options.pop("options")

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
