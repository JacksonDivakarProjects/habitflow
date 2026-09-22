from datetime import date, datetime, timedelta, timezone

import pytest

from app import drafting
from app.timeutil import local_date


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(drafting, "today", lambda: date(2026, 9, 23))


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-09-23", True),
        ("2026-09-22", True),
        ("2026-09-24", False),  # future
        ("TODAY", False),
        ("YESTERDAY", False),
        ("2026-13-01", False),
        ("23-09-2026", False),
        ("", False),
        (None, False),
    ],
)
def test_valid_date(fixed_today, value, expected):
    assert drafting._valid_date(value) is expected


@pytest.mark.parametrize(
    "value, expected",
    [(" Miles ", "miles"), ("KM", "km"), ("", None), ("   ", None), (None, None), (5, "5")],
)
def test_normalize_metric(value, expected):
    assert drafting._normalize_metric(value) == expected


@pytest.mark.parametrize(
    "intent, expected",
    [
        ({"metric": "miles", "suggested_metric": "km"}, ("miles", "explicit")),
        ({"metric": None, "suggested_metric": "km"}, ("km", "suggested")),
        ({"metric": None, "suggested_metric": None}, (None, "missing")),
    ],
)
def test_resolve_metric(intent, expected):
    assert drafting._resolve_metric(intent) == expected


def test_local_date_is_ahead_of_utc_after_ist_midnight():
    # 20:00 UTC on the 22nd is 01:30 IST on the 23rd.
    assert local_date(datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)) == date(2026, 9, 23)


def test_local_date_same_day_before_ist_midnight():
    # 18:00 UTC is 23:30 IST, still the 22nd.
    assert local_date(datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)) == date(2026, 9, 22)


@pytest.mark.parametrize("amount", [0, -1, None])
def test_validate_rejects_non_positive_amount(db, make_intent, amount):
    ok, error, _ = drafting._validate(make_intent(amount=amount), db)
    assert not ok
    assert "amount" in error.lower()


def test_validate_rejects_bad_proposed_habit_name(db, make_intent):
    intent = make_intent(habit_name=None, proposed_habit="learn rust!")
    ok, error, _ = drafting._validate(intent, db)
    assert not ok
    assert "alphanumeric" in error


def test_validate_maps_proposal_to_existing_habit(db, make_intent):
    intent = make_intent(habit_name=None, proposed_habit="Reading")
    ok, _, habit = drafting._validate(intent, db)
    assert ok
    assert habit.name == "reading"
    assert intent["habit_name"] == "reading"
    assert intent["proposed_habit"] is None


def test_validate_rejects_inactive_habit(db, make_intent):
    from sqlalchemy import update

    from app.models import Habit

    db.execute(update(Habit).where(Habit.name == "reels").values(is_active=False))
    db.commit()
    ok, error, _ = drafting._validate(make_intent(habit_name="reels"), db)
    assert not ok
    assert "inactive" in error


def test_validate_rejects_future_date(db, make_intent):
    from app.timeutil import today

    tomorrow = (today() + timedelta(days=1)).isoformat()
    ok, error, _ = drafting._validate(make_intent(log_date=tomorrow), db)
    assert not ok
    assert "Invalid date" in error
