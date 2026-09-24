"""Changing and deleting saved logs: parsing, matching, confirm, apply, undo, edge cases."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.log_edits import find_date, looks_like_edit, parse_request
from app.models import DailyLog, Habit, LogEdit
from app.timeutil import today

REF = date(2026, 9, 24)  # a Thursday
HABITS = [
    {"name": "running", "display_name": "Running"},
    {"name": "reading", "display_name": "Reading"},
    {"name": "work", "display_name": "Work"},
    {"name": "meditation", "display_name": "Meditation"},
]
CHAT = 5


# ------------------------------------------------------------------
# Reading the request (pure)
# ------------------------------------------------------------------
@pytest.mark.parametrize("text,action,habits,day,amount,latest,changes", [
    ("change yesterday's run to 6 km", "edit", ["running"], "2026-09-23", None, False,
     {"amount": 6.0, "unit": "km"}),
    ("Yesterday's reading was 30 pages", "edit", ["reading"], "2026-09-23", None, False,
     {"amount": 30.0, "unit": "pages"}),
    ("move today's meditation to yesterday", "edit", ["meditation"], "2026-09-24", None, False,
     {"log_date": "2026-09-23"}),
    ("delete Monday's reading", "delete", ["reading"], "2026-09-21", None, False, {}),
    ("remove my last run", "delete", ["running"], None, None, True, {}),
    ("change the 5 km run on 21 sep to 6 km", "edit", ["running"], "2026-09-21", 5.0, False,
     {"amount": 6.0, "unit": "km"}),
    ("fix yesterday's run 6 km, not 5", "edit", ["running"], "2026-09-23", None, False,
     {"amount": 6.0, "unit": "km"}),
    ("change yesterday's run to km", "edit", ["running"], "2026-09-23", None, False,
     {"unit": "km"}),
    ("undo work from 2 days ago", "delete", ["work"], "2026-09-22", None, False, {}),
    ("please delete the reading from 22/9", "delete", ["reading"], "2026-09-22", None, False, {}),
    ("update my last reading to 25", "edit", ["reading"], None, None, True, {"amount": 25.0}),
    ("delete the 5 km run", "delete", ["running"], None, 5.0, False, {}),
    ("change worked hours on sep 22 to 7 hours", "edit", ["work"], "2026-09-22", None, False,
     {"amount": 7.0, "unit": "hours"}),
])
def test_parse_request(text, action, habits, day, amount, latest, changes):
    r = parse_request(text, HABITS, REF)
    assert (r.action, r.habits, str(r.log_date) if r.log_date else None, r.amount, r.latest,
            r.changes) == (action, habits, day, amount, latest, changes)


@pytest.mark.parametrize("text,expected", [
    ("2026-09-20", "2026-09-20"),
    ("on 21st september", "2026-09-21"),
    ("sep 21", "2026-09-21"),
    ("21/9", "2026-09-21"),
    ("in december 3", "2025-12-03"),       # a future day this year means last year
    ("day before yesterday", "2026-09-22"),
    ("3 days ago", "2026-09-21"),
    ("thursday", "2026-09-24"),            # today is Thursday
    ("last thursday", "2026-09-17"),
    ("sunday's", "2026-09-20"),
    ("31/2", None),                        # not a date
    ("no date here", None),
])
def test_find_date(text, expected):
    d, _ = find_date(text, REF)
    assert (str(d) if d else None) == expected


def test_date_numbers_are_not_amounts():
    r = parse_request("change the run from 2 days ago to 6 km", HABITS, REF)
    assert r.log_date == date(2026, 9, 22) and r.amount is None and r.changes["amount"] == 6.0


@pytest.mark.parametrize("text,is_edit", [
    ("change yesterday's run to 6 km", True),
    ("delete monday's reading", True),
    ("please remove my last run", True),
    ("can you fix yesterday's run", True),
    ("yesterday's run was 6 km", True),
    ("monday's reading should be 30 pages", True),
    ("ran 5 km", False),
    ("ran 5 km yesterday", False),
    ("how much did I run yesterday", False),
    ("read 20 pages", False),
])
def test_looks_like_edit(text, is_edit):
    assert looks_like_edit(text.lower()) is is_edit


# ------------------------------------------------------------------
# End to end over the API
# ------------------------------------------------------------------
YESTERDAY = today() - timedelta(days=1)


def _log(db, habit, amount, unit, day, voided=False):
    h = db.query(Habit).filter_by(name=habit).one()
    log = DailyLog(habit_id=h.habit_id, amount=amount, metric=unit, log_date=day, source="manual")
    db.add(log)
    db.flush()
    if voided:
        db.execute(text("UPDATE daily_logs SET voided_at = now() WHERE log_id = :i"),
                   {"i": log.log_id})
    db.commit()
    return log.log_id


def ask(client, text_):
    r = client.post("/internal/edits", json={"chat_id": CHAT, "text": text_})
    assert r.status_code == 200, r.text
    return r.json()


def call(client, edit_id, action, **body):
    return client.post(f"/internal/edits/{edit_id}/{action}", json=body or None)


def _row(db, log_id):
    db.expire_all()
    return db.get(DailyLog, log_id)


def test_change_amount_confirm_apply_and_undo(client, db):
    log_id = _log(db, "running", 5, "km", YESTERDAY)

    e = ask(client, "change yesterday's run to 6 km")
    assert (e["status"], e["action"]) == ("pending", "edit")
    assert (e["before"]["quantity"], e["after"]["quantity"]) == ("5 km", "6 km")
    assert e["before"]["when"] == "yesterday"
    assert float(_row(db, log_id).amount) == 5                    # nothing changed yet

    r = call(client, e["edit_id"], "apply")
    assert r.status_code == 200 and r.json()["status"] == "applied"
    row = _row(db, log_id)
    assert (float(row.amount), row.metric, row.metadata_["edits"]) == (6, "km", [e["edit_id"]])

    r = call(client, e["edit_id"], "undo")
    assert r.json()["status"] == "reverted"
    assert float(_row(db, log_id).amount) == 5


def test_unit_only_and_amount_only(client, db):
    log_id = _log(db, "running", 5, "miles", YESTERDAY)
    e = ask(client, "change yesterday's run to km")
    assert e["after"]["quantity"] == "5 km"
    call(client, e["edit_id"], "apply")
    e = ask(client, "yesterday's run was 7")
    assert e["after"]["quantity"] == "7 km"                         # keeps the unit
    call(client, e["edit_id"], "apply")
    assert (float(_row(db, log_id).amount), _row(db, log_id).metric) == (7, "km")


def test_bare_m_is_read_in_the_habits_family(client, db):
    log_id = _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 800 m")
    call(client, e["edit_id"], "apply")
    assert _row(db, log_id).metric == "meters"


def test_move_to_another_day(client, db):
    log_id = _log(db, "meditation", 15, "minutes", today())
    e = ask(client, "move today's meditation to yesterday")
    assert (e["before"]["when"], e["after"]["when"]) == ("today", "yesterday")
    call(client, e["edit_id"], "apply")
    assert _row(db, log_id).log_date == YESTERDAY


def test_delete_and_undo(client, db):
    log_id = _log(db, "reading", 20, "pages", YESTERDAY)
    e = ask(client, "delete yesterday's reading")
    assert (e["status"], e["action"], e["after"]) == ("pending", "delete", None)
    call(client, e["edit_id"], "apply")
    assert _row(db, log_id).voided_at is not None                 # voided, kept for audit
    assert client.get("/internal/today").json()["logs"] == []

    call(client, e["edit_id"], "undo")
    assert _row(db, log_id).voided_at is None


def test_questions_see_the_edit(client, db):
    _log(db, "reading", 20, "pages", today())
    e = ask(client, "change today's reading to 35 pages")
    call(client, e["edit_id"], "apply")
    body = client.post("/internal/ask", json={"chat_id": CHAT, "text": "how much did I read today"})
    assert body.json()["answer"] == "35 pages of Reading today."


def test_several_matches_ask_which_one(client, db):
    first = _log(db, "running", 3, "miles", YESTERDAY)
    second = _log(db, "running", 2, "miles", YESTERDAY)
    e = ask(client, "delete yesterday's run")
    assert e["status"] == "choosing"
    assert [c["log_id"] for c in e["candidates"]] == [second, first]  # newest first
    assert e["candidates"][0]["quantity"] == "2 miles"

    r = call(client, e["edit_id"], "choose", log_id=first)
    assert r.json()["status"] == "pending" and r.json()["before"]["quantity"] == "3 miles"
    call(client, e["edit_id"], "apply")
    assert _row(db, first).voided_at is not None and _row(db, second).voided_at is None


def test_amount_narrows_the_match(client, db):
    _log(db, "running", 3, "miles", YESTERDAY)
    target = _log(db, "running", 5, "miles", YESTERDAY)
    e = ask(client, "delete yesterday's 5 mile run")
    assert e["status"] == "pending" and e["before"]["log_id"] == target


def test_latest_takes_the_most_recent(client, db):
    _log(db, "running", 3, "miles", today() - timedelta(days=3))
    newest = _log(db, "running", 4, "miles", YESTERDAY)
    e = ask(client, "remove my last run")
    assert e["status"] == "pending" and e["before"]["log_id"] == newest


def test_choosing_a_log_that_is_not_offered(client, db):
    _log(db, "running", 3, "miles", YESTERDAY)
    _log(db, "running", 2, "miles", YESTERDAY)
    other = _log(db, "reading", 9, "pages", YESTERDAY)
    e = ask(client, "delete yesterday's run")
    assert call(client, e["edit_id"], "choose", log_id=other).status_code == 422


@pytest.mark.parametrize("text_,message", [
    ("change it to 6 km", "Which log?"),
    ("change yesterday's run", "What should it be?"),
    ("delete yesterday's run", "couldn't find a running yesterday"),
    ("change yesterday's run to 0 km", "more than 0"),
    ("change yesterday's reading to 5 km", "couldn't find"),
])
def test_unclear_or_impossible_requests(client, db, text_, message):
    if "reading" not in text_ and "0 km" in text_:
        _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, text_)
    assert e["status"] == "unclear" and message in e["message"]
    assert db.execute(select(LogEdit)).first() is None


def test_zero_amount_is_refused(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 0 km")
    assert e["status"] == "unclear" and "more than 0" in e["message"]


def test_no_change_is_refused(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 5 km")
    assert e["status"] == "unclear" and "already says 5 km yesterday" in e["message"]


def test_future_date_is_refused(client, db):
    _log(db, "running", 5, "km", today())
    tomorrow = (today() + timedelta(days=1)).isoformat()
    e = ask(client, f"move today's run to {tomorrow}")
    assert e["status"] == "unclear" and "future" in e["message"]


def test_undone_logs_are_not_offered(client, db):
    _log(db, "running", 5, "km", YESTERDAY, voided=True)
    e = ask(client, "delete yesterday's run")
    assert e["status"] == "unclear"


def test_apply_twice_is_idempotent(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 6 km")
    call(client, e["edit_id"], "apply")
    r = call(client, e["edit_id"], "apply")
    assert r.status_code == 200 and r.json()["already"] is True


def test_cancel(client, db):
    log_id = _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "delete yesterday's run")
    assert call(client, e["edit_id"], "cancel").json()["status"] == "cancelled"
    assert call(client, e["edit_id"], "cancel").status_code == 200          # idempotent
    assert call(client, e["edit_id"], "apply").status_code == 409
    assert _row(db, log_id).voided_at is None


def test_a_newer_request_supersedes_the_older(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    old = ask(client, "change yesterday's run to 6 km")
    ask(client, "change yesterday's run to 7 km")
    r = call(client, old["edit_id"], "apply")
    assert r.status_code == 409 and "newer change" in r.json()["detail"]


def test_log_removed_before_applying(client, db):
    log_id = _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 6 km")
    client.post("/internal/undo", json={"log_id": log_id})
    r = call(client, e["edit_id"], "apply")
    assert r.status_code == 409 and "already removed" in r.json()["detail"]


def test_undo_refused_when_the_log_changed_again(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    first = ask(client, "change yesterday's run to 6 km")
    call(client, first["edit_id"], "apply")
    second = ask(client, "change yesterday's run to 7 km")
    call(client, second["edit_id"], "apply")
    r = call(client, first["edit_id"], "undo")
    assert r.status_code == 409 and "changed again" in r.json()["detail"]
    assert call(client, second["edit_id"], "undo").status_code == 200    # latest can


def test_undo_before_apply_and_unknown_edit(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "change yesterday's run to 6 km")
    assert call(client, e["edit_id"], "undo").status_code == 409
    assert call(client, 99999, "apply").status_code == 404


def test_undo_twice(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    e = ask(client, "delete yesterday's run")
    call(client, e["edit_id"], "apply")
    call(client, e["edit_id"], "undo")
    assert call(client, e["edit_id"], "undo").json()["already"] is True


def test_message_router_sends_edits_here(client, db):
    _log(db, "running", 5, "km", YESTERDAY)
    r = client.post("/internal/message", json={"chat_id": CHAT, "text": "change yesterday's run to 6 km"})
    body = r.json()
    assert (body["kind"], body["classified_by"]) == ("edit", "rules")
    assert body["edit"]["after"]["quantity"] == "6 km"
    assert db.execute(text("SELECT count(*) FROM audit_log")).scalar() == 0  # not a new draft
