"""Evening check-in reminders: settings and what gets mentioned."""

from datetime import time, timedelta

import pytest
from sqlalchemy import update

from app.models import DailyLog, Habit
from app.routes.internal import parse_time
from app.timeutil import today

CHAT = 6006


@pytest.mark.parametrize(
    "value, expected",
    [
        ("21:30", time(21, 30)), ("9pm", time(21, 0)), ("9:15 PM", time(21, 15)),
        ("12am", time(0, 0)), ("12pm", time(12, 0)), ("7", time(7, 0)), (" 07:05 ", time(7, 5)),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "9:60", "13pm", "0am", "noon", "", "9.30"])
def test_parse_time_rejects(value):
    with pytest.raises(ValueError):
        parse_time(value)


def _put(client, **body):
    return client.put("/internal/reminders", json={"chat_id": CHAT, **body})


def test_set_update_and_disable(client):
    assert _put(client, remind_at="9pm").json() == {
        "chat_id": CHAT, "remind_at": "21:00", "enabled": True,
    }
    assert _put(client, remind_at="21:30").json()["remind_at"] == "21:30"
    assert client.get("/internal/reminders").json() == [
        {"chat_id": CHAT, "remind_at": "21:30", "enabled": True},
    ]

    off = _put(client, remind_at=None, enabled=False).json()
    assert off == {"chat_id": CHAT, "remind_at": "21:30", "enabled": False}  # time remembered

    assert _put(client, remind_at="21:30").json()["enabled"] is True


def test_disable_when_never_set(client):
    assert _put(client, remind_at=None, enabled=False).json() == {
        "chat_id": CHAT, "remind_at": None, "enabled": False,
    }
    assert client.get("/internal/reminders").json() == []


@pytest.mark.parametrize("body", [{"remind_at": "noon"}, {"remind_at": None, "enabled": True}])
def test_invalid_settings_are_422(client, body):
    assert _put(client, **body).status_code == 422


def _log(db, habit_id, days_ago, voided=False):
    db.add(DailyLog(
        habit_id=habit_id, amount=1, metric="x",
        log_date=today() - timedelta(days=days_ago), source="manual",
    ))
    db.commit()
    if voided:
        db.execute(update(DailyLog).where(DailyLog.habit_id == habit_id)
                   .values(voided_at=today()))
        db.commit()


def test_check_sorts_habits_by_what_needs_attention(client, db):
    # Running: 3-day streak through yesterday, not logged today -> at risk
    for d in (1, 2, 3):
        _log(db, 1, d)
    # Reading: logged today -> done
    _log(db, 2, 0)
    # Meditation: last logged 4 days ago -> not logged (no streak)
    _log(db, 6, 4)
    # Reels: 1-day streak (yesterday) -> at risk
    _log(db, 5, 1)
    # Learning SQL: dormant for 20 days -> not mentioned
    _log(db, 3, 20)
    # Learning Concepts: only a voided log -> not mentioned
    _log(db, 4, 1, voided=True)

    body = client.get("/internal/reminders/check", params={"chat_id": CHAT}).json()

    assert body["at_risk"] == [
        {"habit": "Running", "streak_days": 3},
        {"habit": "Reels", "streak_days": 1},
    ]
    assert body["not_logged"] == ["Meditation"]
    assert body["done"] == ["Reading"]


def test_check_ignores_inactive_habits(client, db):
    _log(db, 1, 1)
    db.execute(update(Habit).where(Habit.habit_id == 1).values(is_active=False))
    db.commit()

    body = client.get("/internal/reminders/check", params={"chat_id": CHAT}).json()

    assert body == {"date": today().isoformat(), "at_risk": [], "not_logged": [], "done": []}
