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
# 5. "m" is meters for runs and minutes for meditation: the habit decides.
#    (First fix dropped the alias, which broke "ran 500 m" on real data.)
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "unit, habit_unit, expected",
    [("m", "miles", "meters"), ("m", "km", "meters"), ("M", "minutes", "minutes"),
     ("m", "hours", "minutes"), ("m", "pages", "m"), ("m", None, "m"), ("km", "minutes", "km")],
)
def test_resolve_reads_m_in_the_habits_family(unit, habit_unit, expected):
    from app.units import resolve

    assert resolve(unit, habit_unit) == expected


def test_parser_keeps_bare_m_for_the_habit_to_decide():
    assert parse_text("meditated 30m").metric == "m"
    assert parse_text("I'm happy, read 12").metric is None  # "m" of I'm is not a unit


@pytest.mark.parametrize(
    "text, preview",
    [("meditated 30m", "30 minutes of Meditation today"),
     ("ran 500m", "500 meters of Running today"),
     ("ran 800 meters", "800 meters of Running today")],
)
def test_offline_m_follows_the_habit(client, fake_llm, text, preview):
    fake_llm.queue(RuntimeError("groq down"))

    r = client.post("/internal/draft", json={"chat_id": CHAT, "text": text})

    assert r.json()["preview"] == preview


def test_llm_answer_in_m_follows_the_habit(client, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(amount=500, metric="m"), "ran 500 m today")

    assert body["preview"] == "500 meters of Running today"


def test_unit_answer_m_follows_the_habit(client, fake_llm, make_intent):
    intent = make_intent(habit_name="meditation", metric=None, amount=30)
    audit_id = _draft(client, fake_llm, intent, "meditated 30")["audit_id"]

    r = _clarify(client, audit_id, "m")

    assert r.json()["preview"] == "30 minutes of Meditation today"


def test_stats_read_old_m_rows_in_context(client, db):
    """Rows stored as "m" before this fix (like a real 'ran 500 m today')."""
    from datetime import timedelta

    from app.models import DailyLog
    from app.timeutil import today

    db.add_all([
        DailyLog(habit_id=1, amount=500, metric="m", log_date=today(), source="llm"),
        DailyLog(habit_id=1, amount=1, metric="miles", log_date=today() - timedelta(days=1),
                 source="llm"),
    ])
    db.commit()

    rows = client.get("/internal/stats", params={"chat_id": CHAT}).json()

    assert [(r["habit"], r["metric"], r["total"]) for r in rows] == [("Running", "miles", 1.31)]


def test_no_habit_rows_created_by_dropped_questions(client, db, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(metric=None), "ran 4")["audit_id"]
    _clarify(client, audit_id, "read 20 pages")

    assert db.execute(select(Habit).where(Habit.name == "pages")).first() is None
