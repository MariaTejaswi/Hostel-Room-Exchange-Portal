import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.database import parse_postgresql_database_url


def test_postgresql_url_decodes_credentials_and_applies_neon_defaults():
    config = parse_postgresql_database_url(
        "postgresql://room%40user:p%40ss%2Fword@ep-example.neon.tech:5433/roomloop_db"
    )

    assert config["NAME"] == "roomloop_db"
    assert config["USER"] == "room@user"
    assert config["PASSWORD"] == "p@ss/word"
    assert config["HOST"] == "ep-example.neon.tech"
    assert config["PORT"] == 5433
    assert config["OPTIONS"]["sslmode"] == "require"
    assert config["OPTIONS"]["options"] == "-c search_path=public"


def test_postgresql_url_preserves_compatible_query_options():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@db.example.test/roomloop_db"
        "?sslmode=verify-full&connect_timeout=10"
        "&options=-c%20statement_timeout%3D5000&ignored=value"
    )

    assert config["OPTIONS"] == {
        "connect_timeout": "10",
        "options": "-c statement_timeout=5000 -c search_path=public",
        "sslmode": "verify-full",
    }


def test_postgresql_url_does_not_duplicate_public_search_path():
    config = parse_postgresql_database_url(
        "postgres://user:secret@db.example.test/roomloop_db"
        "?options=-c%20search_path%3Dpublic"
    )

    assert config["OPTIONS"]["options"] == "-c search_path=public"


def test_non_neon_url_can_explicitly_disable_ssl_for_local_development():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@localhost/roomloop_db?sslmode=disable"
    )

    assert config["OPTIONS"]["sslmode"] == "disable"
    assert config["OPTIONS"]["options"] == "-c search_path=public"


def test_neon_url_cannot_disable_tls():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@ep-example.neon.tech/roomloop_db?sslmode=disable"
    )

    assert config["OPTIONS"]["sslmode"] == "require"


@pytest.mark.parametrize(
    "database_url",
    [
        "mysql://user:secret@db.example.test/roomloop_db",
        "postgresql:///roomloop_db",
        "postgresql://user:secret@db.example.test",
        "postgresql://user:secret@db.example.test:not-a-port/roomloop_db",
    ],
)
def test_invalid_database_urls_fail_closed(database_url):
    with pytest.raises(ImproperlyConfigured):
        parse_postgresql_database_url(database_url)
