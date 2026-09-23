"""Serverless Postgres (Neon) sleeps only when no connection is open."""

import pytest
from sqlalchemy.pool import NullPool, QueuePool

from app.db import make_engine, use_pool

NEON = ("postgresql+psycopg://neondb_owner:pw@ep-cool-bird-a1b2c3.ap-southeast-1.aws.neon.tech"
        "/neondb?sslmode=require&channel_binding=require")


@pytest.mark.parametrize(
    "url, setting, expected",
    [
        (NEON, "auto", False),                                         # Neon: no pool
        ("postgresql+psycopg://u:p@db:5432/hf", "auto", True),         # compose db
        ("postgresql+psycopg://u@/hf?host=/run/postgresql", "auto", True),  # built-in db
        ("postgresql+psycopg://u:p@db:5432/hf", "off", False),         # forced off
        (NEON, "on", True),                                            # forced on
    ],
)
def test_use_pool(url, setting, expected):
    assert use_pool(url, setting) is expected


def test_neon_engine_holds_no_connections_between_requests():
    engine = make_engine(NEON)
    assert isinstance(engine.pool, NullPool)
    assert engine.url.query == {"sslmode": "require", "channel_binding": "require"}


def test_regular_engine_keeps_a_pool():
    assert isinstance(make_engine("postgresql+psycopg://u:p@db:5432/hf").pool, QueuePool)


def test_health_endpoint_does_not_touch_the_database(client, monkeypatch):
    """The container health check calls /health every few seconds."""
    from app import db

    def boom(*a, **k):
        raise AssertionError("health check opened a database session")

    monkeypatch.setattr(db, "SessionLocal", boom)
    assert client.get("/health").json() == {"status": "ok"}
