"""Regressions for the final code-review findings. Each failed before its fix."""

import pytest
from sqlalchemy import select

from app.models import AuditLog, Habit
from app.parser import find_habit, find_unit, parse_correction, parse_text

CHAT = 8008


def _draft(client, fake_llm, intent, text):
    fake_llm.queue(intent)
    r = client.post("/internal/draft", json={"chat_id": CHAT, "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def _clarify(client, audit_id, value):
    return client.post("/internal/clarify", json={"audit_id": audit_id, "value": value})


# ------------------------------------------------------------------
# 1. Offline corrections ignored negation and picked habits in dict order
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("reading, not running", "reading"),
        ("not running, reading", "reading"),
        ("it was reading", "reading"),
        ("ran, then read", "running"),        # earliest mention wins
        ("not running", None),
    ],
)
def test_find_habit_respects_negation_and_order(text, expected):
    assert find_habit(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("not 4 miles, 6 km", "km"), ("km not miles", "km"), ("6 km instead of 4 miles", "km")],
)
def test_find_unit_respects_negation(text, expected):
    assert find_unit(text) == expected


def test_parse_correction_negated_everything():
    assert parse_correction("not 4 miles, 6 km") == {"amount": 6.0, "metric": "km"}


def test_offline_habit_switch_really_switches(client, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(), "ran 4 miles")["audit_id"]
    fake_llm.queue(RuntimeError("groq down"))

    r = client.post("/internal/feedback",
                    json={"audit_id": audit_id, "feedback": "reading, not running"})

    assert r.json()["preview"] == "4 pages of Reading today"


# ------------------------------------------------------------------
# 2. /clarify turned a new log into the old draft's unit
# ------------------------------------------------------------------
def test_new_log_during_unit_question_is_not_a_unit(client, db, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(metric=None, amount=5), "ran 5")["audit_id"]

    r = _clarify(client, audit_id, "read 20 pages")

    assert r.status_code == 409
    assert r.json()["detail"] == {"code": "new_log", "dropped": "ran 5"}
    db.expire_all()
    assert db.get(AuditLog, audit_id).status == "cancelled"
    assert fake_llm.responses == []  # decided without the LLM


def test_new_habit_named_during_unit_question_is_a_new_log(client, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(metric=None, amount=5), "ran 5")["audit_id"]
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric="reps"))

    r = _clarify(client, audit_id, "did 20 pushups")  # parser can't tell; the LLM can

    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "new_log"


def test_unit_answer_mentioning_same_habit_is_fine(client, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(metric=None, amount=5), "ran 5")["audit_id"]

    r = _clarify(client, audit_id, "km, the run")

    assert r.json()["preview"] == "5 km of Running today"


# ------------------------------------------------------------------
# 3. Offline /clarify rejected "km." and "in km"
# ------------------------------------------------------------------
@pytest.mark.parametrize("answer", ["km.", "in km", "KM!", "kilometres"])
def test_short_unit_answers_need_no_llm(client, fake_llm, make_intent, answer):
    audit_id = _draft(client, fake_llm, make_intent(metric=None, amount=5), "ran 5")["audit_id"]

    r = _clarify(client, audit_id, answer)

    assert r.json()["preview"] == "5 km of Running today"
    assert fake_llm.responses == []


def test_offline_custom_unit_word_still_accepted(client, db, fake_llm, make_intent):
    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.commit()
    body = _draft(client, fake_llm, make_intent(habit_name="pushups", metric=None), "did 4 pushups")
    fake_llm.queue(RuntimeError("groq down"))

    r = _clarify(client, body["audit_id"], "sets!")  # a known unit: no LLM needed anyway
    assert r.json()["preview"] == "4 sets of Pushups today"


def test_offline_unknown_unit_word_used_as_typed(client, db, fake_llm, make_intent):
    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.commit()
    body = _draft(client, fake_llm, make_intent(habit_name="pushups", metric=None), "did 4 pushups")
    fake_llm.queue(RuntimeError("groq down"))

    r = _clarify(client, body["audit_id"], "burpees")

    assert r.json()["preview"] == "4 burpees of Pushups today"


# ------------------------------------------------------------------
# 5. "30m" was 30 meters of meditation
# ------------------------------------------------------------------
def test_bare_m_is_not_meters():
    parsed = parse_text("meditated 30m")
    assert parsed.metric is None  # falls back to the habit default (minutes)


def test_offline_meditation_30m_uses_minutes(client, fake_llm):
    fake_llm.queue(RuntimeError("groq down"))

    r = client.post("/internal/draft", json={"chat_id": CHAT, "text": "meditated 30m"})

    assert r.json()["preview"] == "30 minutes of Meditation today"
    assert r.json()["metric_source"] == "suggested"


def test_meters_still_understood_spelled_out():
    assert parse_text("ran 800 meters").metric == "meters"


def test_no_habit_rows_created_by_dropped_questions(client, db, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(metric=None), "ran 4")["audit_id"]
    _clarify(client, audit_id, "read 20 pages")

    assert db.execute(select(Habit).where(Habit.name == "pages")).first() is None
