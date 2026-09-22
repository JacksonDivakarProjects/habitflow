"""
Test harness.

Database: tests run against a throwaway `habitflow_test` database built from
db/init/*.sql. The server comes from TEST_DATABASE_URL if set (e.g. the compose
db), otherwise an embedded Postgres via `pgserver`. The real database is never
touched.

LLM: `app.drafting.call_llm` is replaced by FakeLLM, which returns queued
responses so every flow is deterministic.
"""

import copy
import os
import tempfile
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.engine import make_url

INIT_DIR = Path(__file__).resolve().parents[2] / "db" / "init"
TEST_DB = "habitflow_test"

_pg_server = None  # keep the embedded server alive for the whole session


def _server_url():
    global _pg_server
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        return make_url(url)
    try:
        import pgserver
    except ImportError:
        pytest.exit("Set TEST_DATABASE_URL or `pip install pgserver`.", returncode=2)
    # Fresh cluster per session, deleted on exit: a killed run can't leave a
    # half-recovered data dir behind for the next one.
    data_dir = Path(tempfile.mkdtemp(prefix="habitflow-test-pg-"))
    _pg_server = pgserver.get_server(data_dir, cleanup_mode="delete")
    return make_url(_pg_server.get_uri())


def _libpq(url) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def pytest_configure(config):
    base = _server_url()
    with psycopg.connect(_libpq(base.set(database="postgres")), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {TEST_DB}")

    test_url = base.set(database=TEST_DB)
    with psycopg.connect(_libpq(test_url)) as conn:
        conn.execute((INIT_DIR / "01_schema.sql").read_text())

    # Must be set before `app` is imported: app.config reads it at import time.
    os.environ["DATABASE_URL"] = test_url.set(
        drivername="postgresql+psycopg"
    ).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def migrated():
    from app.db import engine
    from app.migrate import run_migrations

    run_migrations(engine)


@pytest.fixture(autouse=True)
def clean_db(migrated):
    from app.db import engine

    seed = (INIT_DIR / "02_seed.sql").read_text()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "TRUNCATE daily_logs, audit_log, habits, reminder_settings RESTART IDENTITY CASCADE"
        )
        conn.exec_driver_sql(seed)
    yield


@pytest.fixture
def db():
    from app.db import SessionLocal

    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


class FakeLLM:
    def __init__(self):
        self.responses: list = []
        self.calls: list[dict] = []

    def queue(self, *responses):
        self.responses.extend(responses)

    def __call__(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(f"FakeLLM got an unexpected call: {kwargs}")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return copy.deepcopy(r)


@pytest.fixture
def fake_llm(monkeypatch):
    from app import drafting

    fake = FakeLLM()
    monkeypatch.setattr(drafting, "call_llm", fake)
    return fake


def draft_sql_for(habit_name: str) -> str:
    return (
        "INSERT INTO daily_logs (habit_id, amount, metric, log_date, source)\n"
        f"VALUES ((SELECT habit_id FROM habits WHERE name = '{habit_name}'),\n"
        "        :amount, :metric, :log_date, 'llm')"
    )


@pytest.fixture
def make_intent():
    """Build a valid LLM intent (4 miles of running, today); override any field."""
    from app.timeutil import today

    def _make(**overrides) -> dict:
        habit = overrides.get("habit_name", "running") or overrides.get(
            "proposed_habit", "unknown"
        )
        intent = {
            "habit_name": "running",
            "proposed_habit": None,
            "amount": 4,
            "metric": "miles",
            "suggested_metric": None,
            "log_date": today().isoformat(),
            "confidence": 0.9,
            "draft_sql": draft_sql_for(habit),
        }
        intent.update(overrides)
        return intent

    return _make
