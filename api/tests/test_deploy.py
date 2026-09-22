"""
The schema comes only from api/migrations (no db/init mount), so every kind
of database a deployment can meet must end up identical:
  - a brand-new empty Postgres (fresh server pulling the images),
  - one created by the old db/init scripts,
  - one created by db/init that already ran 0001/0002 (an existing install).
"""

import shutil
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.migrate import MIGRATIONS_DIR, run_migrations

LEGACY_SQL = (Path(__file__).parent / "fixtures" / "legacy_db_init.sql").read_text()
ALL_VERSIONS = sorted(p.stem for p in MIGRATIONS_DIR.glob("*.sql"))


@contextmanager
def scratch_database(name: str):
    """A throwaway database next to the test one; yields a SQLAlchemy engine."""
    import os

    url = make_url(os.environ["DATABASE_URL"])
    libpq = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(libpq, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {name}")
    engine = create_engine(url.set(database=name))
    try:
        yield engine
    finally:
        engine.dispose()
        with psycopg.connect(libpq, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _raw(engine, sql: str):
    """Run multi-statement SQL the way docker-entrypoint-initdb.d does."""
    libpq = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(libpq) as conn:
        conn.execute(sql)


def _state(engine) -> dict:
    with engine.connect() as c:
        return {
            "habits": c.execute(text("SELECT count(*) FROM habits")).scalar(),
            "logs": c.execute(text("SELECT count(*) FROM daily_logs")).scalar(),
            "has_voided_at": c.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'daily_logs' AND column_name = 'voided_at'")).scalar() == 1,
            "has_reminders": c.execute(text(
                "SELECT to_regclass('reminder_settings') IS NOT NULL")).scalar(),
            "versions": c.execute(text(
                "SELECT version FROM schema_migrations ORDER BY version")).scalars().all(),
        }


def test_migration_files_start_with_the_baseline():
    assert ALL_VERSIONS[:2] == ["0000_baseline", "0000_seed_habits"]


def test_fresh_empty_database_gets_everything():
    with scratch_database("habitflow_fresh") as engine:
        assert run_migrations(engine) == ALL_VERSIONS
        state = _state(engine)
        assert (state["habits"], state["has_voided_at"], state["has_reminders"]) == (6, True, True)
        assert run_migrations(engine) == []


def test_database_created_by_old_db_init_upgrades_cleanly():
    with scratch_database("habitflow_legacy") as engine:
        _raw(engine, LEGACY_SQL)
        _raw(engine, "INSERT INTO daily_logs (habit_id, amount, metric, log_date) "
                     "VALUES (1, 500, 'm', CURRENT_DATE)")

        assert run_migrations(engine) == ALL_VERSIONS

        state = _state(engine)
        assert state["habits"] == 6     # seed didn't duplicate the starter habits
        assert state["logs"] == 1       # user data kept
        assert state["has_voided_at"] and state["has_reminders"]


def test_existing_install_that_already_ran_0001_and_0002(tmp_path):
    """The live setup: db/init + 0001 + 0002 applied by the previous release."""
    for version in ("0001_daily_logs_voided_at", "0002_reminder_settings"):
        shutil.copy(MIGRATIONS_DIR / f"{version}.sql", tmp_path)
    with scratch_database("habitflow_existing") as engine:
        _raw(engine, LEGACY_SQL)
        assert run_migrations(engine, tmp_path) == [
            "0001_daily_logs_voided_at", "0002_reminder_settings",
        ]
        _raw(engine, "INSERT INTO reminder_settings (chat_id, remind_at) VALUES (42, '21:00')")

        assert run_migrations(engine) == ["0000_baseline", "0000_seed_habits"]

        state = _state(engine)
        assert state["versions"] == ALL_VERSIONS
        assert state["habits"] == 6
        with engine.connect() as c:
            assert c.execute(text("SELECT chat_id FROM reminder_settings")).scalars().all() == [42]


@pytest.mark.parametrize("path", sorted(MIGRATIONS_DIR.glob("*.sql")), ids=lambda p: p.stem)
def test_every_migration_is_idempotent(path):
    """Running any migration twice must be harmless (they're re-run on old installs)."""
    with scratch_database("habitflow_twice") as engine:
        run_migrations(engine)
        _raw(engine, path.read_text())  # second time, outside the runner
        assert _state(engine)["habits"] == 6
