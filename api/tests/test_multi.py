"""Several habits in one message: "ran 3 miles and read 20 pages"."""

from datetime import timedelta

from sqlalchemy import select

from app import drafting
from app.models import DailyLog
from app.sqlguard import check_draft_sql
from app.timeutil import today

CHAT = 5005
TODAY = today().isoformat()


def _extra(habit="reading", amount=20, metric="pages", **kw):
    return {"habit_name": habit, "amount": amount, "metric": metric, "log_date": TODAY, **kw}


def _draft(client, fake_llm, intent, text="ran 3 miles and read 20 pages"):
    fake_llm.queue(intent)
    return client.post("/internal/draft", json={"chat_id": CHAT, "text": text}).json()


def test_two_habits_one_card(client, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(amount=3, extra_logs=[_extra()]))

    assert body["preview"] == "3 miles of Running today\n20 pages of Reading today"
    assert body["skipped"] == []


def test_approve_logs_every_habit_and_undo_reverts_all(client, db, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(amount=3, extra_logs=[_extra()]))["audit_id"]

    r = client.post("/internal/execute", json={"audit_id": audit_id}).json()

    assert [s["habit"] for s in r["summaries"]] == ["Running", "Reading"]
    assert len(r["log_ids"]) == 2
    logs = db.execute(select(DailyLog).order_by(DailyLog.log_id)).scalars().all()
    assert [(float(x.amount), x.metric, x.audit_id) for x in logs] == [
        (3.0, "miles", audit_id), (20.0, "pages", audit_id),
    ]

    undo = client.post("/internal/undo", json={"audit_id": audit_id}).json()
    assert undo["status"] == "undone"
    assert undo["preview"] == "3 miles of Running today and 20 pages of Reading today"
    assert client.get("/internal/today").json()["logs"] == []


def test_unknown_extra_habit_is_skipped_with_reason(client, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(extra_logs=[_extra(habit="pushups", metric="reps")]))

    assert body["preview"] == "4 miles of Running today"
    assert body["skipped"] == ["Pushups (new habit, send it on its own)"]


def test_extra_without_unit_uses_habit_default(client, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(extra_logs=[_extra(metric=None)]))

    assert body["preview"].endswith("20 pages of Reading today")


def test_extra_uses_suggested_unit(client, fake_llm, make_intent):
    extra = _extra(habit="meditation", amount=10, metric=None, suggested_metric="mins")
    body = _draft(client, fake_llm, make_intent(extra_logs=[extra]))

    assert body["preview"].endswith("10 minutes of Meditation today")


def test_extra_needing_unit_is_skipped(client, db, fake_llm, make_intent):
    from app.models import Habit

    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.commit()
    body = _draft(client, fake_llm, make_intent(extra_logs=[_extra(habit="pushups", metric=None)]))

    assert body["skipped"] == ["Pushups (needs a unit, send it on its own)"]


def test_invalid_extras_are_skipped(client, fake_llm, make_intent):
    tomorrow = (today() + timedelta(days=1)).isoformat()
    extras = [
        _extra(amount=0),
        _extra(habit="meditation", metric="minutes", log_date=tomorrow),
        "not a dict",
    ]
    body = _draft(client, fake_llm, make_intent(extra_logs=extras))

    assert body["preview"] == "4 miles of Running today"
    assert body["skipped"] == ["Reading (no amount)", "Meditation (date isn't valid)"]


def test_extra_defaults_to_main_date(client, fake_llm, make_intent):
    yesterday = (today() - timedelta(days=1)).isoformat()
    extra = _extra()
    del extra["log_date"]
    body = _draft(client, fake_llm, make_intent(log_date=yesterday, extra_logs=[extra]))

    assert body["preview"] == "4 miles of Running yesterday\n20 pages of Reading yesterday"


def test_extras_are_capped(client, fake_llm, make_intent):
    extras = [_extra(amount=i + 1) for i in range(drafting.MAX_EXTRA_LOGS + 3)]
    body = _draft(client, fake_llm, make_intent(extra_logs=extras))

    assert len(body["preview"].splitlines()) == 1 + drafting.MAX_EXTRA_LOGS


def test_extra_logs_not_a_list_is_ignored(client, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(extra_logs="reading 20"))

    assert body["preview"] == "4 miles of Running today"


def test_main_needs_unit_extras_wait_for_it(client, db, fake_llm, make_intent):
    body = _draft(client, fake_llm, make_intent(metric=None, extra_logs=[_extra()]))
    assert body["needs_input"] == "unit"
    assert body["prompt"].endswith("(+1 more log in this message)")

    r = client.post("/internal/clarify", json={"audit_id": body["audit_id"], "value": "km"})
    assert r.json()["preview"] == "4 km of Running today\n20 pages of Reading today"

    client.post("/internal/execute", json={"audit_id": body["audit_id"]})
    assert len(db.execute(select(DailyLog)).scalars().all()) == 2


def test_new_main_habit_with_known_extra(client, fake_llm, make_intent):
    intent = make_intent(
        habit_name=None, proposed_habit="pushups", metric="reps", extra_logs=[_extra()]
    )
    body = _draft(client, fake_llm, intent, text="20 pushups and read 20 pages")
    assert body["prompt"].endswith("(+1 more log in this message)")

    r = client.post("/internal/approve_habit", json={"audit_id": body["audit_id"], "accept": True})
    assert r.json()["preview"] == "4 reps of Pushups today\n20 pages of Reading today"


def test_multi_row_sql_passes_guard_and_dry_run(db):
    sql = drafting.template_sql(["running", "reading", "meditation"])

    assert check_draft_sql(sql) is None
    assert drafting._dry_run_sql(db, sql) is None


def test_llm_view_shows_extras_without_bookkeeping(client, fake_llm, make_intent):
    audit_id = _draft(client, fake_llm, make_intent(extra_logs=[_extra()]))["audit_id"]
    fake_llm.queue(make_intent())

    client.post("/internal/feedback", json={"audit_id": audit_id, "feedback": "drop reading"})

    extras = fake_llm.calls[-1]["current_draft"]["extra_logs"]
    assert extras == [{"habit_name": "reading", "amount": 20.0, "metric": "pages", "log_date": TODAY}]
