import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.database import parse_postgresql_database_url


def test_pooled_neon_url_removes_search_path_startup_option():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@ep-example-pooler.neon.tech/roomloop_db"
        "?sslmode=require"
        "&options=-c%20search_path%3Dpublic%20-c%20statement_timeout%3D5000"
    )

    assert config["OPTIONS"] == {
        "options": "-c statement_timeout=5000",
        "sslmode": "require",
    }


def test_pooled_neon_url_drops_empty_options_after_removing_search_path():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@ep-example-pooler.neon.tech/roomloop_db"
        "?options=-csearch_path%3Dpublic"
    )

    assert config["OPTIONS"] == {"sslmode": "require"}


def test_pooled_neon_url_without_options_parameter():
    config = parse_postgresql_database_url(
        "postgresql://neondb_owner:npg_secret@ep-cool-pool-123456-pooler.us-east-2.aws.neon.tech/roomloop_db"
    )

    assert config["HOST"] == "ep-cool-pool-123456-pooler.us-east-2.aws.neon.tech"
    assert config["OPTIONS"] == {"sslmode": "require"}
    assert "options" not in config["OPTIONS"]


def test_direct_neon_url_preserves_explicit_options():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@ep-example.neon.tech/roomloop_db"
        "?options=-c%20search_path%3Dpublic%20-c%20statement_timeout%3D5000"
    )

    assert config["OPTIONS"] == {
        "options": "-c search_path=public -c statement_timeout=5000",
        "sslmode": "require",
    }


def test_direct_neon_url_without_options_parameter():
    config = parse_postgresql_database_url(
        "postgresql://neondb_owner:secret@ep-example-direct.neon.tech/roomloop_db"
    )

    assert config["HOST"] == "ep-example-direct.neon.tech"
    assert config["OPTIONS"] == {"sslmode": "require"}
    assert "options" not in config["OPTIONS"]


def test_neon_url_decodes_credentials_and_enforces_tls():
    config = parse_postgresql_database_url(
        "postgresql://room%40user:p%40ss%2Fword@ep-example.neon.tech:5433/roomloop_db"
        "?sslmode=disable"
    )

    assert config["NAME"] == "roomloop_db"
    assert config["USER"] == "room@user"
    assert config["PASSWORD"] == "p@ss/word"
    assert config["HOST"] == "ep-example.neon.tech"
    assert config["PORT"] == 5433
    assert config["OPTIONS"] == {"sslmode": "require"}


def test_neon_url_preserves_strong_ssl_modes():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@ep-example.neon.tech/roomloop_db?sslmode=verify-full"
    )

    assert config["OPTIONS"]["sslmode"] == "verify-full"


def test_url_decodes_complex_special_characters():
    config = parse_postgresql_database_url(
        "postgresql://user%2Bname%3A1:p%40ss%23word%24@localhost:5432/roomloop_db"
    )

    assert config["USER"] == "user+name:1"
    assert config["PASSWORD"] == "p@ss#word$"


def test_url_preserves_compatible_query_parameters():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@db.example.test/roomloop_db"
        "?sslmode=verify-full&connect_timeout=10&channel_binding=require"
        "&application_name=roomloop&options=-c%20statement_timeout%3D5000"
    )

    assert config["OPTIONS"] == {
        "application_name": "roomloop",
        "channel_binding": "require",
        "connect_timeout": "10",
        "options": "-c statement_timeout=5000",
        "sslmode": "verify-full",
    }


def test_local_postgresql_defaults_to_prefer_ssl():
    config = parse_postgresql_database_url(
        "postgresql://postgres:localpass@localhost:5432/roomloop_db"
    )

    assert config["HOST"] == "localhost"
    assert config["PORT"] == 5432
    assert config["NAME"] == "roomloop_db"
    assert config["USER"] == "postgres"
    assert config["PASSWORD"] == "localpass"
    assert config["OPTIONS"] == {"sslmode": "prefer"}


def test_local_postgresql_can_disable_ssl_without_injected_options():
    config = parse_postgresql_database_url(
        "postgresql://user:secret@localhost/roomloop_db?sslmode=disable"
    )

    assert config["OPTIONS"] == {"sslmode": "disable"}


def test_unsupported_query_parameter_fails_closed():
    with pytest.raises(ImproperlyConfigured, match="unsupported PostgreSQL"):
        parse_postgresql_database_url(
            "postgresql://user:secret@db.example.test/roomloop_db?unknown=value"
        )


def test_malformed_options_parameter_fails_closed():
    with pytest.raises(ImproperlyConfigured, match="malformed PostgreSQL options"):
        parse_postgresql_database_url(
            'postgresql://user:secret@localhost/roomloop_db?options="-c unclosed'
        )


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
