"""
Minimal forward-only migrations.

The whole schema lives in api/migrations/NNNN_name.sql, starting with
0000_baseline.sql, so an empty Postgres needs nothing mounted. Each file is
applied once, in order, at API startup, and recorded in schema_migrations.
Write them idempotently (IF NOT EXISTS) so a fresh database, one created by
the old db/init, and an upgraded one all end up the same.
"""

import logging
from pathlib import Path

from sqlalchemy import Engine, text

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_ID = 0x4AB17F10  # pg advisory lock: one migrator at a time

log = logging.getLogger("habitflow.migrate")


def run_migrations(engine: Engine, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations; return the versions applied this run."""
    applied_now = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        done = set(conn.execute(text("SELECT version FROM schema_migrations")).scalars())
        for path in sorted(directory.glob("*.sql")):
            if path.stem in done:
                continue
            log.info("applying migration %s", path.stem)
            conn.exec_driver_sql(path.read_text())
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": path.stem}
            )
            applied_now.append(path.stem)
    return applied_now
