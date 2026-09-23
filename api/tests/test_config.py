"""DATABASE_URL as hosting providers hand it out (Render, Heroku, ...)."""

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@host:5432/db", "postgresql+psycopg://u:p@host:5432/db"),
        ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql+psycopg://u:p@db:5432/db", "postgresql+psycopg://u:p@db:5432/db"),
        ("postgresql+psycopg://u@/db?host=/run/postgresql",
         "postgresql+psycopg://u@/db?host=/run/postgresql"),  # the all-in-one image's socket
    ],
)
def test_database_url_gets_the_psycopg_driver(given, expected):
    assert Settings(database_url=given).database_url == expected
