"""Bug 7: the keyword blocklist let destructive SQL through to EXPLAIN."""

from pathlib import Path

import pytest
from sqlalchemy import select, text

from app import drafting
from app.models import Habit
from app.sqlguard import bind_nulls, check_draft_sql
from tests.conftest import draft_sql_for

GOOD = [
    draft_sql_for("running"),
    "INSERT INTO daily_logs (habit_id, amount, metric, log_date) VALUES (1, :amount, :metric, :log_date)",
    "INSERT INTO public.daily_logs (habit_id, amount, metric, log_date) VALUES (1, 1, 'x', CURRENT_DATE)",
    "INSERT INTO daily_logs (habit_id, amount, metric, log_date, metadata) VALUES (1, 1, 'x', NOW()::date, '{}'::jsonb)",
    "insert into daily_logs (habit_id, amount, metric, log_date)\nselect habit_id, 1, 'x', current_date from habits where name = 'reading'",
    draft_sql_for("running") + " RETURNING log_id",
]

BAD = {
    "second statement": draft_sql_for("running") + "; DROP TABLE habits",
    "newline delete": "DELETE\nFROM habits",
    "no-space drop": "INSERT INTO daily_logs (amount) VALUES (1);drop table habits",
    "explain analyze trick": "ANALYZE INSERT INTO daily_logs (amount) VALUES (1)",
    "data-modifying CTE": "WITH d AS (DELETE FROM habits RETURNING habit_id) INSERT INTO daily_logs (habit_id) SELECT habit_id FROM d",
    "upsert": "INSERT INTO daily_logs (log_id) VALUES (1) ON CONFLICT (log_id) DO UPDATE SET amount = 0",
    "pg_sleep": "INSERT INTO daily_logs (amount) VALUES (pg_sleep(10))",
    "file read": "INSERT INTO daily_logs (metric) VALUES (pg_read_file('/etc/passwd'))",
    "wrong target": "INSERT INTO habits (name, display_name) VALUES ('x', 'x')",
    "other schema": "INSERT INTO evil.daily_logs (amount) VALUES (1)",
    "reads audit_log": "INSERT INTO daily_logs (metric) SELECT user_input FROM audit_log",
    "select only": "SELECT * FROM habits",
    "update": "UPDATE daily_logs SET amount = 0",
    "truncate": "TRUNCATE daily_logs",
    "grant": "GRANT ALL ON habits TO public",
    "garbage": "SELEC nonsense",
    "empty": ";",
}


@pytest.mark.parametrize("sql", GOOD)
def test_accepts_plain_insert(sql):
    assert check_draft_sql(sql) is None


@pytest.mark.parametrize("sql", BAD.values(), ids=BAD.keys())
def test_rejects(sql):
    assert check_draft_sql(sql) is not None


def test_bind_nulls_keeps_casts():
    assert bind_nulls("VALUES (:amount, '{}'::jsonb, :log_date::date)") == (
        "VALUES (NULL, '{}'::jsonb, NULL::date)"
    )


@pytest.mark.parametrize("sql", GOOD)
def test_good_sql_passes_dry_run(db, sql):
    assert drafting._dry_run_sql(db, sql) is None


def test_rejected_sql_never_reaches_database(db, monkeypatch):
    executed = []
    real_execute = db.execute
    monkeypatch.setattr(db, "execute", lambda *a, **k: executed.append(a) or real_execute(*a, **k))

    error = drafting._dry_run_sql(db, BAD["data-modifying CTE"])

    assert error is not None
    assert executed == []
    assert db.execute(select(Habit)).first() is not None


def test_bad_sql_from_llm_is_retried(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(draft_sql=BAD["second statement"]), make_intent())

    r = client.post("/internal/draft", json={"chat_id": 7, "text": "ran 4 miles"})

    assert r.status_code == 200
    assert "exactly one statement" in fake_llm.calls[1]["previous_error"]


def test_habits_table_intact_after_attack_attempts(client, db, fake_llm, make_intent):
    fake_llm.queue(*[make_intent(draft_sql=sql) for sql in list(BAD.values())[:3]])

    client.post("/internal/draft", json={"chat_id": 7, "text": "ran 4 miles"})

    assert db.execute(text("SELECT count(*) FROM habits")).scalar() == 6


def _semantics_sql_examples():
    import yaml

    path = Path(__file__).resolve().parents[2] / "llm" / "semantics.yaml"
    return [ex["draft_sql"] for ex in yaml.safe_load(path.read_text())["sql_examples"]]


@pytest.mark.parametrize("sql", _semantics_sql_examples())
def test_prompt_examples_pass_the_guard(db, sql):
    """The SQL we show the LLM as an example must be SQL we accept."""
    assert check_draft_sql(sql) is None
    assert drafting._dry_run_sql(db, sql) is None
