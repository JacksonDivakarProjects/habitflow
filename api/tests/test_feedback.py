"""Loop 2: correcting a draft with ✏️ Edit. Every path a correction can take."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, DailyLog, Habit
from app.timeutil import friendly_date, today

CHAT = 4004
YESTERDAY = (today() - timedelta(days=1)).isoformat()
TOMORROW = (today() + timedelta(days=1)).isoformat()
TWO_DAYS_AGO = friendly_date(today() - timedelta(days=2))


@pytest.fixture
def draft(client, fake_llm, make_intent):
    """Create an open draft from `intent` (default: pending 4 miles of running)."""
    def _draft(intent=None, text="ran 4 miles"):
        fake_llm.queue(intent or make_intent())
        r = client.post("/internal/draft", json={"chat_id": CHAT, "text": text})
        assert r.status_code == 200, r.text
        return r.json()

    return _draft


def _feedback(client, audit_id, text):
    return client.post("/internal/feedback", json={"audit_id": audit_id, "feedback": text})


def _audit(db, audit_id) -> AuditLog:
    db.expire_all()
    return db.get(AuditLog, audit_id)


# ------------------------------------------------------------------
# Successful corrections
# ------------------------------------------------------------------
def test_correct_amount(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(amount=6))

    r = _feedback(client, audit_id, "6 miles, not 4")

    assert r.status_code == 200
    assert r.json()["audit_id"] == audit_id  # same draft, edited in place
    assert r.json()["preview"] == "6 miles of Running today"
    audit = _audit(db, audit_id)
    assert audit.status == "pending"
    assert audit.intent["feedback_history"] == ["6 miles, not 4"]
    assert audit.user_feedback == "6 miles, not 4"
    assert audit.iteration_count == 2


def test_correct_date(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(log_date=YESTERDAY))

    r = _feedback(client, audit_id, "it was yesterday")

    assert r.json()["preview"] == "4 miles of Running yesterday"


def test_correct_unit(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(metric="KM"))

    r = _feedback(client, audit_id, "km not miles")

    assert r.json()["preview"] == "4 km of Running today"  # unit normalized


def test_correct_habit(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(habit_name="reading", metric=None, suggested_metric="pages"))

    r = _feedback(client, audit_id, "wrong habit, it was reading")

    assert r.json()["preview"] == "4 pages of Reading today"
    assert r.json()["metric_source"] == "suggested"


def test_several_corrections_accumulate(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(amount=6))
    _feedback(client, audit_id, "6 not 4")
    fake_llm.queue(make_intent(amount=6, log_date=YESTERDAY))

    r = _feedback(client, audit_id, "and it was yesterday")

    assert r.json()["preview"] == "6 miles of Running yesterday"
    audit = _audit(db, audit_id)
    assert audit.intent["feedback_history"] == ["6 not 4", "and it was yesterday"]
    assert audit.iteration_count == 3
    # The second call showed the LLM the already-corrected draft.
    assert fake_llm.calls[-1]["current_draft"]["amount"] == 6


def test_approve_after_correction_logs_corrected_values(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(amount=6, log_date=YESTERDAY))
    _feedback(client, audit_id, "6, yesterday")

    client.post("/internal/execute", json={"audit_id": audit_id})

    log = db.execute(select(DailyLog)).scalar_one()
    assert (float(log.amount), log.log_date.isoformat()) == (6.0, YESTERDAY)
    assert log.raw_input == "ran 4 miles"  # the original message is kept


def test_correction_adds_second_habit(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(extra_logs=[
        {"habit_name": "reading", "amount": 10, "metric": "pages", "log_date": today().isoformat()},
    ]))

    r = _feedback(client, audit_id, "also read 10 pages")

    assert r.json()["preview"] == "4 miles of Running today\n10 pages of Reading today"


# ------------------------------------------------------------------
# Corrections that change what the draft needs
# ------------------------------------------------------------------
def test_correction_to_new_habit_asks_to_create_it(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="swimming", metric="laps"))

    r = _feedback(client, audit_id, "no, it was swimming, 4 laps")

    assert r.json()["needs_input"] == "habit"
    assert _audit(db, audit_id).status == "awaiting_input"

    r = client.post("/internal/approve_habit", json={"audit_id": audit_id, "accept": True})
    assert r.json()["preview"] == "4 laps of Swimming today"


def test_correction_that_drops_the_unit_asks_for_it(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(metric=None))

    r = _feedback(client, audit_id, "not miles")

    assert r.json()["needs_input"] == "unit"
    r = client.post("/internal/clarify", json={"audit_id": audit_id, "value": "km"})
    assert r.json()["preview"] == "4 km of Running today"


def test_correct_a_draft_waiting_for_its_unit(client, fake_llm, make_intent, draft):
    audit_id = draft(make_intent(metric=None), text="ran 4")["audit_id"]
    fake_llm.queue(make_intent(amount=5, metric="km"))

    r = _feedback(client, audit_id, "actually 5 km")

    assert r.json()["preview"] == "5 km of Running today"


def test_correct_a_new_habit_proposal_into_a_known_habit(client, db, fake_llm, make_intent, draft):
    audit_id = draft(make_intent(habit_name=None, proposed_habit="bookreading"))["audit_id"]
    fake_llm.queue(make_intent(habit_name="reading", metric="pages"))

    r = _feedback(client, audit_id, "I meant my normal reading habit")

    assert r.json()["preview"] == "4 pages of Reading today"
    assert db.execute(select(Habit).where(Habit.name == "bookreading")).first() is None


def test_resolving_a_correction_supersedes_another_pending_draft(
    client, db, fake_llm, make_intent, draft
):
    waiting = draft(make_intent(metric=None), text="ran 4")["audit_id"]
    newer = draft(make_intent(amount=9))["audit_id"]
    fake_llm.queue(make_intent(metric="km"))

    _feedback(client, waiting, "4 km")

    assert (_audit(db, waiting).status, _audit(db, newer).status) == ("pending", "superseded")


# ------------------------------------------------------------------
# Corrections that can't be applied: the draft must survive
# ------------------------------------------------------------------
def test_failed_correction_keeps_draft_intact(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(*[make_intent(amount=0)] * 3)

    r = _feedback(client, audit_id, "make it zero")

    assert r.status_code == 422
    audit = _audit(db, audit_id)
    assert audit.status == "pending"                 # still approvable
    assert audit.intent["amount"] == 4               # unchanged
    assert audit.user_feedback == "make it zero"     # attempt recorded
    assert audit.intent["feedback_history"] == ["make it zero"]
    assert "Loop 2 failed" in audit.error_message
    assert audit.duration_ms is not None

    r = client.post("/internal/execute", json={"audit_id": audit_id})
    assert r.json()["summary"]["amount"] == 4


def test_failed_correction_can_be_retried(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(*[make_intent(amount=0)] * 3)
    _feedback(client, audit_id, "make it zero")
    fake_llm.queue(make_intent(amount=2))

    r = _feedback(client, audit_id, "ok, 2 miles")

    assert r.json()["preview"] == "2 miles of Running today"


def test_future_date_correction_is_refused(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(*[make_intent(log_date=TOMORROW)] * 3)

    r = _feedback(client, audit_id, "log it for tomorrow")

    assert r.status_code == 422
    assert _audit(db, audit_id).intent["log_date"] == today().isoformat()


def test_malicious_sql_in_correction_is_rejected(client, db, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    evil = make_intent(amount=6, draft_sql="DELETE FROM habits")
    fake_llm.queue(evil, make_intent(amount=6))

    r = _feedback(client, audit_id, "6")

    assert r.status_code == 200
    assert "DELETE" in fake_llm.calls[-1]["previous_error"]
    assert db.execute(select(Habit)).first() is not None


def test_retry_shows_llm_its_rejected_answer(client, fake_llm, make_intent, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(make_intent(amount="lots"), make_intent(amount=6))

    _feedback(client, audit_id, "6")

    first, second = fake_llm.calls[-2], fake_llm.calls[-1]
    assert "previous_intent" not in first
    assert second["previous_intent"]["amount"] == "lots"
    assert "positive number" in second["previous_error"]
    assert second["current_draft"]["amount"] == 4  # still the real draft


# ------------------------------------------------------------------
# LLM unavailable: simple corrections still work
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "correction, preview",
    [
        ("6 miles, not 4", "6 miles of Running today"),
        ("no, 6", "6 miles of Running today"),
        ("it was 5km", "5 km of Running today"),
        ("it was yesterday", "4 miles of Running yesterday"),
        ("2 days ago", f"4 miles of Running {TWO_DAYS_AGO}"),  # 2 is a date, not an amount
        ("wrong habit, it was reading", "4 pages of Reading today"),
    ],
)
def test_offline_corrections(client, fake_llm, draft, correction, preview):
    audit_id = draft()["audit_id"]
    fake_llm.queue(RuntimeError("groq down"))

    r = _feedback(client, audit_id, correction)

    assert r.status_code == 200, r.text
    assert r.json()["preview"] == preview
    assert r.json()["offline"] is True
    assert len(fake_llm.calls) == 2  # the draft, then one failed call: no pointless retries


def test_offline_unparseable_correction_keeps_draft(client, db, fake_llm, draft):
    audit_id = draft()["audit_id"]
    fake_llm.queue(*[RuntimeError("groq down")] * 3)

    r = _feedback(client, audit_id, "hmm, not quite")

    assert r.status_code == 422
    assert _audit(db, audit_id).status == "pending"


# ------------------------------------------------------------------
# Corrections on drafts that are no longer open
# ------------------------------------------------------------------
@pytest.mark.parametrize("close", ["execute", "discard", "supersede"])
def test_correction_on_closed_draft_is_409(client, fake_llm, make_intent, draft, close):
    audit_id = draft()["audit_id"]
    if close == "execute":
        client.post("/internal/execute", json={"audit_id": audit_id})
    elif close == "discard":
        client.post("/internal/discard", json={"audit_id": audit_id})
    else:
        draft(make_intent(amount=5))

    r = _feedback(client, audit_id, "6")

    assert r.status_code == 409
    assert fake_llm.responses == []  # no LLM call was made


def test_correction_on_unknown_draft_is_404(client):
    assert _feedback(client, 999, "6").status_code == 404
