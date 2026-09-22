"""End-to-end API flows with a fake LLM, one test per state path."""

from sqlalchemy import select

from app.models import AuditLog, DailyLog, Habit

CHAT = 1001


def _draft(client, text="ran 4 miles"):
    return client.post("/internal/draft", json={"chat_id": CHAT, "text": text})


def _audits(db):
    db.expire_all()
    return db.execute(select(AuditLog).order_by(AuditLog.audit_id)).scalars().all()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/db").json() == {"db": "ok"}


def test_list_habits_returns_seeded_habits(client):
    names = {h["name"] for h in client.get("/internal/habits").json()}
    assert names == {
        "running", "reading", "learning_sql", "learning_concepts", "reels", "meditation",
    }


def test_draft_then_execute_writes_log(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())

    r = _draft(client)
    assert r.status_code == 200
    body = r.json()
    assert body["preview"] == "4 miles of Running today"
    assert body["metric_source"] == "explicit"
    assert "INSERT INTO daily_logs" in body["draft_sql"]

    r = client.post("/internal/execute", json={"audit_id": body["audit_id"]})
    assert r.status_code == 200
    assert r.json()["status"] == "executed"

    log = db.execute(select(DailyLog)).scalar_one()
    assert (float(log.amount), log.metric, log.source) == (4.0, "miles", "llm")
    assert log.audit_id == body["audit_id"]
    assert log.raw_input == "ran 4 miles"
    assert _audits(db)[-1].status == "executed"


def test_execute_is_idempotent(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    client.post("/internal/execute", json={"audit_id": audit_id})
    r = client.post("/internal/execute", json={"audit_id": audit_id})

    assert r.json() == {
        "status": "already_executed", "log_id": 1, "log_ids": [1],
        "summary": None, "summaries": [],
    }
    assert len(db.execute(select(DailyLog)).scalars().all()) == 1


def test_execute_unknown_audit_is_404(client):
    assert client.post("/internal/execute", json={"audit_id": 999}).status_code == 404


def test_suggested_metric_is_used_and_flagged(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None, suggested_metric="miles"))

    body = _draft(client, "ran 4").json()

    assert body["preview"].startswith("4 miles of Running")
    assert body["metric_source"] == "suggested"


def test_missing_unit_then_clarify(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None, suggested_metric=None))
    r = _draft(client, "ran 4")
    body = r.json()
    assert body["needs_input"] == "unit"
    assert "What unit?" in body["prompt"]
    assert _audits(db)[-1].status == "awaiting_input"

    fake_llm.queue(make_intent(metric="km"))
    r = client.post(
        "/internal/clarify", json={"audit_id": body["audit_id"], "value": "hmm I think it was km"}
    )

    assert r.status_code == 200
    assert r.json()["preview"].startswith("4 km of Running")
    assert _audits(db)[-1].status == "pending"
    call = fake_llm.calls[-1]
    assert call["original_text"] == "ran 4"
    assert call["clarification"] == "hmm I think it was km"
    running = next(h for h in call["habits"] if h["name"] == "running")
    assert running["default_metric"] == "miles"


def test_clarify_on_pending_audit_is_409(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    r = client.post("/internal/clarify", json={"audit_id": audit_id, "value": "km"})

    assert r.status_code == 409


def test_new_habit_proposal_approved_then_executed(client, db, fake_llm, make_intent):
    fake_llm.queue(
        make_intent(habit_name=None, proposed_habit="Learning Rust", metric="hours")
    )
    body = _draft(client, "learned rust for 4 hours").json()
    assert body["needs_input"] == "habit"
    assert body["prompt"] == (
        "“Learning Rust” is a new habit. Create it and log 4 hours today?"
    )

    r = client.post(
        "/internal/approve_habit", json={"audit_id": body["audit_id"], "accept": True}
    )
    assert r.status_code == 200
    assert r.json()["preview"].startswith("4 hours of Learning Rust")

    habit = db.execute(select(Habit).where(Habit.name == "learning_rust")).scalar_one()
    assert (habit.display_name, habit.metric) == ("Learning Rust", "hours")

    r = client.post("/internal/execute", json={"audit_id": body["audit_id"]})
    assert r.json()["status"] == "executed"


def test_new_habit_without_unit_asks_for_unit(client, fake_llm, make_intent):
    fake_llm.queue(
        make_intent(habit_name=None, proposed_habit="pushups", metric=None)
    )
    audit_id = _draft(client, "did 20 pushups").json()["audit_id"]

    r = client.post("/internal/approve_habit", json={"audit_id": audit_id, "accept": True})
    assert r.json()["needs_input"] == "unit"

    fake_llm.queue(make_intent(habit_name="pushups", metric="reps"))
    r = client.post("/internal/clarify", json={"audit_id": audit_id, "value": "reps"})
    assert r.status_code == 200
    assert r.json()["preview"].startswith("4 reps of Pushups")


def test_new_habit_declined_is_cancelled(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups"))
    audit_id = _draft(client).json()["audit_id"]

    r = client.post("/internal/approve_habit", json={"audit_id": audit_id, "accept": False})

    assert r.json()["preview"] == "Cancelled."
    assert _audits(db)[-1].status == "cancelled"
    assert db.execute(select(Habit).where(Habit.name == "pushups")).first() is None


def test_feedback_regenerates_same_audit(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    fake_llm.queue(make_intent(amount=6))
    r = client.post(
        "/internal/feedback", json={"audit_id": audit_id, "feedback": "no, 6 miles"}
    )

    assert r.status_code == 200
    assert r.json()["audit_id"] == audit_id
    assert r.json()["preview"].startswith("6 miles of Running")
    audit = _audits(db)[-1]
    assert (audit.status, audit.user_feedback, audit.iteration_count) == (
        "pending", "no, 6 miles", 2,
    )


def test_feedback_shows_llm_its_current_draft(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]

    fake_llm.queue(make_intent(amount=6))
    client.post("/internal/feedback", json={"audit_id": audit_id, "feedback": "6"})

    current = fake_llm.calls[-1]["current_draft"]
    assert current["amount"] == 4
    assert current["habit_name"] == "running"
    # Our bookkeeping keys stay out of the prompt.
    assert not {"habit_id", "attempts", "metric_source", "needs"} & current.keys()


def test_clarify_shows_llm_its_current_draft(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    audit_id = _draft(client, "ran 4").json()["audit_id"]

    fake_llm.queue(make_intent(metric="km"))
    client.post("/internal/clarify", json={"audit_id": audit_id, "value": "kilometres I think, not sure"})

    current = fake_llm.calls[-1]["current_draft"]
    assert (current["amount"], current["metric"]) == (4, None)


def test_draft_records_message_id_and_duration(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())

    client.post(
        "/internal/draft", json={"chat_id": CHAT, "text": "ran 4", "message_id": 555}
    )

    audit = _audits(db)[-1]
    assert audit.message_id == 555
    assert audit.duration_ms is not None and audit.duration_ms >= 0


def test_feedback_on_executed_audit_is_409(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    audit_id = _draft(client).json()["audit_id"]
    client.post("/internal/execute", json={"audit_id": audit_id})

    r = client.post("/internal/feedback", json={"audit_id": audit_id, "feedback": "x"})

    assert r.status_code == 409


def test_llm_down_and_unparseable_returns_422(client, db, fake_llm):
    fake_llm.queue(*[RuntimeError("groq down")] * 3)

    r = _draft(client, "did the thing")

    assert r.status_code == 422
    assert "failed after 3 attempts" in r.json()["detail"]
    assert len(fake_llm.calls) == 3
    assert _audits(db)[-1].status == "failed"


def test_invalid_intent_retries_with_error_then_succeeds(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(amount=0), make_intent())

    r = _draft(client)

    assert r.status_code == 200
    assert "positive" in fake_llm.calls[1]["previous_error"]
    assert [a.status for a in _audits(db)] == ["failed", "pending"]


def test_unknown_habit_name_is_offered_as_new_habit(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name="Swimming", metric="laps"))

    body = _draft(client, "swam 4 laps").json()

    assert body["needs_input"] == "habit"
    assert body["prompt"].startswith("“Swimming” is a new habit.")
    assert len(fake_llm.calls) == 1  # no retry round trip


def test_new_draft_supersedes_previous_pending(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(), make_intent(amount=5))
    first = _draft(client).json()["audit_id"]
    _draft(client, "ran 5 miles")

    assert [a.status for a in _audits(db)] == ["superseded", "pending"]
    r = client.post("/internal/execute", json={"audit_id": first})
    assert r.status_code == 409


def test_stats_sums_last_30_days(client, fake_llm, make_intent):
    for amount in (4, 2):
        fake_llm.queue(make_intent(amount=amount))
        audit_id = _draft(client).json()["audit_id"]
        client.post("/internal/execute", json={"audit_id": audit_id})

    rows = client.get("/internal/stats", params={"chat_id": CHAT}).json()

    assert rows == [
        {"habit": "Running", "metric": "miles", "total": 6.0, "days": 1, "streak_days": 1}
    ]
