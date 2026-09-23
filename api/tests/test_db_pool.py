"""Serverless Postgres (Neon) sleeps only when no connection is open."""

import os
import time

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool, QueuePool

from app.db import make_engine, pool_mode, use_pool

NEON = ("postgresql+psycopg://neondb_owner:pw@ep-cool-bird-a1b2c3.ap-southeast-1.aws.neon.tech"
        "/neondb?sslmode=require&channel_binding=require")
NEON_POOLER = NEON.replace("a1b2c3.", "a1b2c3-pooler.")


@pytest.mark.parametrize(
    "url, setting, expected",
    [
        (NEON, "auto", "idle"),                                         # Neon: close when idle
        (NEON_POOLER, "auto", "idle"),
        ("postgresql+psycopg://u:p@db:5432/hf", "auto", "pool"),        # compose db
        ("postgresql+psycopg://u@/hf?host=/run/postgresql", "auto", "pool"),  # built-in db
        ("postgresql+psycopg://u:p@db:5432/hf", "off", "none"),
        (NEON, "on", "pool"),
        ("postgresql+psycopg://u:p@db:5432/hf", "idle", "idle"),
    ],
)
def test_pool_mode(url, setting, expected):
    assert pool_mode(url, setting) == expected
    assert use_pool(url, setting) is (expected != "none")


def test_neon_engine_keeps_tls_settings():
    engine = make_engine(NEON)
    assert isinstance(engine.pool, QueuePool)
    assert engine.url.query == {"sslmode": "require", "channel_binding": "require"}


def test_pool_off_means_a_connection_per_checkout():
    assert isinstance(make_engine("postgresql+psycopg://u:p@db/hf", "off").pool, NullPool)


def test_idle_pool_reuses_while_busy_then_closes():
    """Against the real test database: connections are reused during a burst,
    then all closed once idle, which is what lets Neon scale to zero."""
    engine = make_engine(os.environ["DATABASE_URL"], "idle", idle_seconds=1)
    try:
        pids = set()
        for _ in range(5):
            with engine.connect() as conn:
                pids.add(conn.execute(text("SELECT pg_backend_pid()")).scalar())
        assert len(pids) == 1                      # one connection reused for the burst
        assert engine.pool.checkedin() == 1

        deadline = time.monotonic() + 5
        while engine.pool.checkedin() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert engine.pool.checkedin() == 0        # closed after the quiet period
        with engine.connect() as conn:             # and it reconnects on the next use
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()


def test_health_endpoint_does_not_touch_the_database(client, monkeypatch):
    """The container health check calls /health every few seconds."""
    from app import db

    def boom(*a, **k):
        raise AssertionError("health check opened a database session")

    monkeypatch.setattr(db, "SessionLocal", boom)
    assert client.get("/health").json() == {"status": "ok"}
