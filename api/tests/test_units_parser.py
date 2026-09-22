"""Unit names/conversion, the regex parser, and converted /stats."""

from datetime import timedelta

import pytest

from app.models import DailyLog
from app.parser import parse_correction, parse_text
from app.timeutil import today
from app.units import canonical, convert, is_known

TODAY = today().isoformat()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Miles", "miles"), ("mi", "miles"), ("KMs", "km"), ("kilometres", "km"),
        ("Mins", "minutes"), ("min.", "minutes"), ("hrs", "hours"), ("pgs", "pages"),
        ("laps", "laps"), ("  ", None), (None, None),
    ],
)
def test_canonical(raw, expected):
    assert canonical(raw) == expected


@pytest.mark.parametrize(
    "amount, src, dst, expected",
    [
        (1, "miles", "km", 1.609344),
        (5, "km", "mi", 5 / 1.609344),
        (90, "mins", "hours", 1.5),
        (2, "hours", "minutes", 120),
        (1500, "m", "km", 1.5),
        (500, "ml", "liters", 0.5),
        (3, "pages", "pages", 3),
        (3, "pages", "km", None),
        (3, "laps", "km", None),
    ],
)
def test_convert(amount, src, dst, expected):
    result = convert(amount, src, dst)
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_is_known():
    assert is_known("KM") and is_known("mins")
    assert not is_known("yes")


# ------------------------------------------------------------------
# parse_text
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, habit, amount, metric, days_ago",
    [
        ("ran 4 miles", "running", 4, "miles", 0),
        ("Ran 5km this morning", "running", 5, "km", 0),
        ("jogged 3.5 mi yesterday", "running", 3.5, "miles", 1),
        ("read 20 pages 2 days ago", "reading", 20, "pages", 2),
        ("meditated for 15 mins", "meditation", 15, "minutes", 0),
        ("I'm happy, read 12", "reading", 12, None, 0),  # "m" in I'm is not meters
        ("sql for 2 hrs the day before yesterday", "learning_sql", 2, "hours", 2),
    ],
)
def test_parse_text(text, habit, amount, metric, days_ago):
    parsed = parse_text(text)
    assert (parsed.habit_name, float(parsed.amount), parsed.metric) == (habit, amount, metric)
    assert parsed.log_date == (today() - timedelta(days=days_ago)).isoformat()


@pytest.mark.parametrize("text", ["walked the dog", "ran far", "will run 5 miles tomorrow", ""])
def test_parse_text_gives_up(text):
    assert parse_text(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("6 miles, not 4", {"amount": 6.0, "metric": "miles"}),
        ("not 4 but 6", {"amount": 6.0}),
        ("it was yesterday", {"log_date": (today() - timedelta(days=1)).isoformat()}),
        ("wrong habit, it was reading", {"habit_name": "reading"}),
        ("5km instead of 4", {"amount": 5.0, "metric": "km"}),
        ("2 days ago", {"log_date": (today() - timedelta(days=2)).isoformat()}),
        ("3 miles, 2 days ago", {"amount": 3.0, "metric": "miles",
                                 "log_date": (today() - timedelta(days=2)).isoformat()}),
        ("hmm", {}),
    ],
)
def test_parse_correction(text, expected):
    assert parse_correction(text) == expected


# ------------------------------------------------------------------
# /stats converts to each habit's default unit
# ------------------------------------------------------------------
def _log(db, habit_id, amount, metric, days_ago=0):
    db.add(DailyLog(
        habit_id=habit_id, amount=amount, metric=metric,
        log_date=today() - timedelta(days=days_ago), source="manual",
    ))
    db.commit()


def test_stats_combine_compatible_units(client, db):
    _log(db, 1, 5, "km")               # Running's default is miles
    _log(db, 1, 1, "miles", days_ago=1)
    _log(db, 6, 1, "hours")            # Meditation's default is minutes
    _log(db, 6, 15, "minutes")
    _log(db, 1, 30, "minutes")         # unrelated unit: its own row

    rows = client.get("/internal/stats", params={"chat_id": 1}).json()

    assert [(r["habit"], r["metric"], r["total"], r["days"]) for r in rows] == [
        ("Meditation", "minutes", 75.0, 1),
        ("Running", "miles", 4.11, 2),
        ("Running", "minutes", 30.0, 1),
    ]


@pytest.mark.parametrize(
    "amount, unit, expected",
    [(1, "miles", "1 mile"), (2, "miles", "2 miles"), (1.5, "hours", "1.5 hours"),
     (1, "km", "1 km"), (1, "glasses", "1 glass"), (1, "laps", "1 lap"), (1, "xyz", "1 xyz")],
)
def test_quantity(amount, unit, expected):
    from app.units import quantity

    assert quantity(amount, unit) == expected
