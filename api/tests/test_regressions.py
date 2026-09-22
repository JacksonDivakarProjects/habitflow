"""Regression tests for bugs found in review. Each one failed before its fix."""

import pytest
from sqlalchemy import select

from app.models import AuditLog

CHAT = 2002


def _draft(client, text="ran 4 miles"):
    return client.post("/internal/draft", json={"chat_id": CHAT, "text": text})


def _statuses(db):
    db.expire_all()
    rows = db.execute(select(AuditLog).order_by(AuditLog.audit_id)).scalars().all()
    return [a.status for a in rows]


# ------------------------------------------------------------------
# Bug 1: a failing EXPLAIN left the transaction aborted, so the retry
# loop crashed with InFailedSqlTransaction instead of retrying.
# ------------------------------------------------------------------
def test_bad_draft_sql_is_retried_not_500(client, db, fake_llm, make_intent):
    broken = make_intent(draft_sql="INSERT INTO daily_log (x) VALUES (:amount)")
    fake_llm.queue(broken, make_intent())

    r = _draft(client)

    assert r.status_code == 200
    assert r.json()["preview"].startswith("4 miles of Running")
    assert "draft_sql failed" in fake_llm.calls[1]["previous_error"]
    assert _statuses(db) == ["failed", "pending"]


def test_bad_draft_sql_during_feedback_is_retried(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    fake_llm.queue(make_intent(draft_sql="SELEC nonsense"), make_intent(amount=6))
    r = client.post("/internal/feedback", json={"audit_id": audit_id, "feedback": "6"})

    assert r.status_code == 200
    assert r.json()["preview"].startswith("6 miles")


# ------------------------------------------------------------------
# Bug 2: /approve_habit, /clarify and /feedback moved an awaiting_input
# row to pending without superseding the chat's other pending row,
# violating uq_audit_pending_per_chat (IntegrityError -> 500).
# ------------------------------------------------------------------
@pytest.fixture
def newer_pending(client, fake_llm, make_intent):
    def _make():
        fake_llm.queue(make_intent(amount=9))
        r = _draft(client, "ran 9 miles")
        assert r.status_code == 200
        return r.json()["audit_id"]

    return _make


def test_approve_old_habit_card_after_newer_draft(
    client, db, fake_llm, make_intent, newer_pending
):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric="reps"))
    old = _draft(client, "20 pushups").json()["audit_id"]
    newer_pending()

    r = client.post("/internal/approve_habit", json={"audit_id": old, "accept": True})

    assert r.status_code == 200
    assert _statuses(db) == ["pending", "superseded"]


def test_clarify_old_unit_question_after_newer_draft(
    client, db, fake_llm, make_intent, newer_pending
):
    fake_llm.queue(make_intent(metric=None))
    old = _draft(client, "ran 4").json()["audit_id"]
    newer_pending()

    fake_llm.queue(make_intent(metric="km"))
    r = client.post("/internal/clarify", json={"audit_id": old, "value": "km"})

    assert r.status_code == 200
    assert _statuses(db) == ["pending", "superseded"]


def test_feedback_on_old_awaiting_audit_after_newer_draft(
    client, db, fake_llm, make_intent, newer_pending
):
    fake_llm.queue(make_intent(metric=None))
    old = _draft(client, "ran 4").json()["audit_id"]
    newer_pending()

    fake_llm.queue(make_intent(amount=5))
    r = client.post("/internal/feedback", json={"audit_id": old, "feedback": "5 miles"})

    assert r.status_code == 200
    assert _statuses(db) == ["pending", "superseded"]


# ------------------------------------------------------------------
# Bug 3: non-numeric amounts crashed with ValueError (500); numeric
# strings passed validation and then crashed the `:g` preview format.
# ------------------------------------------------------------------
@pytest.mark.parametrize("bad_amount", ["four", "4 miles", "NaN", "inf", [4]])
def test_non_numeric_amount_is_retried(client, db, fake_llm, make_intent, bad_amount):
    fake_llm.queue(make_intent(amount=bad_amount), make_intent())

    r = _draft(client)

    assert r.status_code == 200
    assert "amount" in fake_llm.calls[1]["previous_error"].lower()
    assert _statuses(db) == ["failed", "pending"]


def test_numeric_string_amount_is_accepted(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(amount="4.5"))

    r = _draft(client)

    assert r.status_code == 200
    assert r.json()["preview"].startswith("4.5 miles of Running")
    assert r.json()["intent"]["amount"] == 4.5


def test_numeric_string_amount_on_habit_proposal(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", amount="20"))

    r = _draft(client)

    assert r.status_code == 200
    assert "log 20" in r.json()["prompt"]
