"""Regression tests for the Postgres driver pin (app.core.config).

SQLAlchemy 2.1 changed the default driver for a bare ``postgresql://`` URL to
psycopg (v3), which this project does not ship (it uses psycopg2-binary). The
`normalize_postgres_driver` helper pins the driver to psycopg2 so the engine
is deterministic across SQLAlchemy versions.
"""

from app.core.config import normalize_postgres_driver


def test_bare_postgres_url_is_pinned_to_psycopg2():
    assert (
        normalize_postgres_driver("postgresql://u:p@localhost:5432/db")
        == "postgresql+psycopg2://u:p@localhost:5432/db"
    )


def test_already_qualified_psycopg2_url_untouched():
    url = "postgresql+psycopg2://u:p@localhost:5432/db"
    assert normalize_postgres_driver(url) == url


def test_asyncpg_url_untouched():
    url = "postgresql+asyncpg://u:p@localhost:5432/db"
    assert normalize_postgres_driver(url) == url


def test_sqlite_url_untouched():
    assert normalize_postgres_driver("sqlite:///:memory:") == "sqlite:///:memory:"
