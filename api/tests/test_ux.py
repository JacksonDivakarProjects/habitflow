"""Undo, discard, streaks, /today, friendly wording and the offline fallback."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app import progress
from app.migrate import run_migrations
from app.models import AuditLog, DailyLog
from app.timeutil import friendly_date, today

CHAT = 3003
RUNNING = 1  # seeded habit ids follow 02_seed.sql order
READING = 2


def _draft(client, text="ran 4 miles"):
    return client.post("/internal/draft", json={"chat_id": CHAT, "text": text})


def _log(db, days_ago=0, amount=1, metric="miles", habit_id=RUNNING, voided=False):
    row = DailyLog(
        habit_id=habit_id,
        amount=amount,
        metric=metric,
        log_date=today() - timedelta(days=days_ago),
        source="manual",
    )
    db.add(row)
    db.flush()
    if voided:
        db.execute(
            text("UPDATE daily_logs SET voided_at = NOW() WHERE log_id = :id"),
            {"id": row.log_id},
        )
    db.commit()
    return row


# ------------------------------------------------------------------
# Friendly dates
# ------------------------------------------------------------------
REF = date(2026, 9, 23)  # a Wednesday


@pytest.mark.parametrize(
    "value, expected",
    [
        (REF, "today"),
        ("2026-09-22", "yesterday"),
        ("2026-09-21", "on Mon 21 Sep"),
        ("2026-09-05", "on Sat 5 Sep"),
        ("2025-12-31", "on Wed 31 Dec 2025"),
    ],
)
def test_friendly_date(value, expected):
    assert friendly_date(value, ref=REF) == expected


# ------------------------------------------------------------------
# Streaks and weekly totals
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "days_ago, expected",
    [
        ([0, 1, 2], 3),
        ([0, 1, 3], 2),       # gap breaks it
        ([1, 2], 2),          # not logged today yet: streak still alive
        ([2, 3], 0),          # missed yesterday: gone
        ([0, 0, 0], 1),       # several logs on one day count once
        ([], 0),
    ],
)
def test_streak_days(db, days_ago, expected):
    for d in days_ago:
        _log(db, days_ago=d)
    assert progress.streak_days(db, RUNNING) == expected


def test_streak_ignores_voided_and_other_habits(db):
    _log(db, 0)
    _log(db, 1, voided=True)
    _log(db, 2)
    _log(db, 1, habit_id=READING, metric="pages")
    assert progress.streak_days(db, RUNNING) == 1


def test_week_total_is_per_unit_this_week_and_live_only(db):
    monday = today() - timedelta(days=today().weekday())
    days_since_monday = (today() - monday).days
    _log(db, 0, amount=3)
    _log(db, days_since_monday, amount=2)       # Monday counts
    _log(db, days_since_monday + 1, amount=50)  # last Sunday does not
    _log(db, 0, amount=8.05, metric="km")       # converts: ~5 miles
    _log(db, 0, amount=30, metric="minutes")    # can't convert to miles: left out
    _log(db, 0, amount=7, voided=True)

    assert progress.week_total(db, RUNNING, "miles") == 10.0
    assert progress.week_total(db, RUNNING, "km") == 16.1  # 5 mi = 8.05 km, + 8.05 km


def test_execute_returns_progress_summary(client, db, fake_llm, make_intent):
    _log(db, 1, amount=2)
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    r = client.post("/internal/execute", json={"audit_id": audit_id})

    summary = r.json()["summary"]
    assert summary["habit"] == "Running"
    assert (summary["amount"], summary["metric"]) == (4, "miles")
    assert summary["log_date"] == today().isoformat()
    assert summary["when"] == "today"
    assert summary["streak_days"] == 2
    expected_week = 6 if today().weekday() > 0 else 4  # yesterday is last week on Mondays
    assert summary["week_total"] == expected_week


# ------------------------------------------------------------------
# Undo
# ------------------------------------------------------------------
def test_undo_by_log_id(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]
    log_id = client.post("/internal/execute", json={"audit_id": audit_id}).json()["log_id"]

    r = client.post("/internal/undo", json={"log_id": log_id})

    assert r.json() == {
        "status": "undone", "log_id": log_id, "log_ids": [log_id],
        "preview": "4 miles of Running today",
    }
    db.expire_all()
    assert db.get(DailyLog, log_id).voided_at is not None
    # Kept for the audit trail, but gone from stats and today.
    assert client.get("/internal/stats", params={"chat_id": CHAT}).json() == []
    assert client.get("/internal/today").json()["logs"] == []


def test_undo_twice_is_idempotent(client, db):
    row = _log(db)
    client.post("/internal/undo", json={"log_id": row.log_id})

    r = client.post("/internal/undo", json={"log_id": row.log_id})

    assert r.json()["status"] == "already_undone"


def test_undo_without_id_takes_latest_live_log(client, db):
    older = _log(db, 1, amount=1)
    newer = _log(db, 0, amount=2)

    first = client.post("/internal/undo", json={}).json()
    second = client.post("/internal/undo", json={}).json()

    assert (first["log_id"], second["log_id"]) == (newer.log_id, older.log_id)
    assert client.post("/internal/undo", json={}).status_code == 404


def test_undo_unknown_log_is_404(client):
    assert client.post("/internal/undo", json={"log_id": 999}).status_code == 404


def test_undone_unit_is_not_suggested_as_recent(client, db, fake_llm, make_intent):
    _log(db, 0, metric="km", voided=True)
    fake_llm.queue(make_intent())

    _draft(client)

    running = next(h for h in fake_llm.calls[0]["habits"] if h["name"] == "running")
    assert running["recent_metric"] is None


# ------------------------------------------------------------------
# Discard
# ------------------------------------------------------------------
def test_discard_pending_draft(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    assert client.post("/internal/discard", json={"audit_id": audit_id}).json() == {
        "status": "discarded"
    }
    assert client.post("/internal/discard", json={"audit_id": audit_id}).json() == {
        "status": "already_discarded"
    }
    assert client.post("/internal/execute", json={"audit_id": audit_id}).status_code == 409


def test_discard_awaiting_unit(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    audit_id = _draft(client, "ran 4").json()["audit_id"]

    client.post("/internal/discard", json={"audit_id": audit_id})

    db.expire_all()
    assert db.get(AuditLog, audit_id).status == "cancelled"


def test_cannot_discard_executed_draft(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]
    client.post("/internal/execute", json={"audit_id": audit_id})

    r = client.post("/internal/discard", json={"audit_id": audit_id})

    assert r.status_code == 409
    assert r.json()["detail"] == "Audit status is executed"


# ------------------------------------------------------------------
# /today
# ------------------------------------------------------------------
def test_today_lists_only_todays_live_logs(client, db):
    _log(db, 0, amount=3)
    _log(db, 0, amount=20, metric="pages", habit_id=READING)
    _log(db, 1, amount=9)
    _log(db, 0, amount=8, voided=True)

    body = client.get("/internal/today").json()

    assert body["date"] == today().isoformat()
    assert [(x["habit"], x["amount"], x["metric"]) for x in body["logs"]] == [
        ("Running", 3.0, "miles"),
        ("Reading", 20.0, "pages"),
    ]


def test_stats_include_streak(client, db):
    _log(db, 0)
    _log(db, 1)

    rows = client.get("/internal/stats", params={"chat_id": CHAT}).json()

    assert rows[0]["streak_days"] == 2


# ------------------------------------------------------------------
# Wording
# ------------------------------------------------------------------
def test_unit_prompt_is_friendly(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))

    body = _draft(client, "ran 4").json()

    assert body["prompt"] == "Got it: 4 of Running today. What unit? (e.g. miles)"


def test_new_habit_prompt_without_unit(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric=None))

    body = _draft(client, "did 20 pushups").json()

    assert body["prompt"] == (
        "“Pushups” is a new habit. Create it and log 4 today? I'll ask for the unit next."
    )


# ------------------------------------------------------------------
# Offline fallback: the LLM is down
# ------------------------------------------------------------------
def test_llm_down_falls_back_to_regex_parser(client, db, fake_llm):
    fake_llm.queue(RuntimeError("groq down"))

    r = _draft(client, "ran 5 miles yesterday")

    body = r.json()
    assert r.status_code == 200
    assert body["offline"] is True
    assert body["preview"] == "5 miles of Running yesterday"
    assert len(fake_llm.calls) == 1  # no pointless retries once the parser succeeded


def test_offline_fallback_uses_habit_default_unit(client, fake_llm):
    fake_llm.queue(RuntimeError("groq down"))

    body = _draft(client, "meditated 10").json()

    assert body["preview"] == "10 minutes of Meditation today"
    assert body["metric_source"] == "suggested"


def test_online_drafts_are_not_marked_offline(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    assert _draft(client).json()["offline"] is False


def test_clarify_with_llm_down_accepts_a_bare_unit(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    audit_id = _draft(client, "ran 4").json()["audit_id"]

    fake_llm.queue(RuntimeError("groq down"))
    r = client.post("/internal/clarify", json={"audit_id": audit_id, "value": "KM"})

    assert r.status_code == 200
    assert r.json()["preview"] == "4 km of Running today"


def test_clarify_with_llm_down_rejects_a_sentence(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    audit_id = _draft(client, "ran 4").json()["audit_id"]

    fake_llm.queue(RuntimeError("groq down"))
    r = client.post(
        "/internal/clarify", json={"audit_id": audit_id, "value": "the long way round"}
    )

    assert r.json()["needs_input"] == "unit"
    assert "didn't catch a unit" in r.json()["prompt"]


# ------------------------------------------------------------------
# Migrations
# ------------------------------------------------------------------
def test_migrations_are_recorded_and_idempotent(db):
    from app.db import engine

    assert run_migrations(engine) == []
    versions = db.execute(text("SELECT version FROM schema_migrations")).scalars().all()
    assert "0001_daily_logs_voided_at" in versions


def test_migration_runner_applies_new_files_in_order(db, tmp_path):
    from app.db import engine

    (tmp_path / "0002_b.sql").write_text(
        "CREATE TABLE IF NOT EXISTS mig_test_b (id INT);\n"
        "INSERT INTO mig_test_b VALUES (1);"
    )
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE IF NOT EXISTS mig_test_a (id INT);")
    try:
        assert run_migrations(engine, tmp_path) == ["0001_a", "0002_b"]
        assert run_migrations(engine, tmp_path) == []
        assert db.execute(text("SELECT id FROM mig_test_b")).scalar() == 1
    finally:
        db.rollback()  # release the session's lock on mig_test_b before DROP
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS mig_test_a, mig_test_b")
            conn.exec_driver_sql(
                "DELETE FROM schema_migrations WHERE version IN ('0001_a', '0002_b')"
            )


def test_voided_at_column_exists(db):
    assert db.execute(select(DailyLog.voided_at)).all() == []
