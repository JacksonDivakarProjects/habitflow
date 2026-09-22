"""Streaks, totals and reminders. Voided logs never count. Totals convert
between compatible units (km/miles, minutes/hours) via app.units."""

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DailyLog, Habit
from app.timeutil import friendly_date, today
from app.units import convert, resolve

RECENT_DAYS = 14  # reminders skip habits untouched for longer than this


def _live():
    return DailyLog.voided_at.is_(None)


def _logged_days(db: Session, habit_id: int, ref: date) -> set[date]:
    return set(
        db.execute(
            select(DailyLog.log_date)
            .where(DailyLog.habit_id == habit_id, _live(), DailyLog.log_date <= ref)
            .distinct()
        ).scalars()
    )


def streak_days(db: Session, habit_id: int, ref: Optional[date] = None) -> int:
    """Consecutive days with a log, ending today, or yesterday if today isn't logged yet."""
    ref = ref or today()
    days = _logged_days(db, habit_id, ref)
    day = ref if ref in days else ref - timedelta(days=1)
    streak = 0
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def week_total(
    db: Session, habit_id: int, metric: str, ref: Optional[date] = None,
    habit_unit: Optional[str] = None,
) -> float:
    """This ISO week's (Mon to ref) total for the habit, in `metric`.
    Logs in convertible units are converted; others are left out."""
    ref = ref or today()
    monday = ref - timedelta(days=ref.weekday())
    rows = db.execute(
        select(DailyLog.amount, DailyLog.metric).where(
            DailyLog.habit_id == habit_id,
            DailyLog.log_date.between(monday, ref),
            _live(),
        )
    ).all()
    total = 0.0
    for amount, unit in rows:
        converted = convert(float(amount), resolve(unit, habit_unit or metric), metric)
        if converted is not None:
            total += converted
    return round(total, 2)


def log_summary(db: Session, log: DailyLog, habit: Habit) -> dict:
    log_date = log.log_date if isinstance(log.log_date, date) else date.fromisoformat(
        str(log.log_date)
    )
    return {
        "log_id": log.log_id,
        "habit": habit.display_name,
        "amount": float(log.amount),
        "metric": log.metric,
        "log_date": log_date.isoformat(),
        "when": friendly_date(log_date),
        "streak_days": streak_days(db, habit.habit_id),
        "week_total": week_total(db, habit.habit_id, log.metric, habit_unit=habit.metric),
    }


def habit_stats(db: Session, since: date) -> list[dict]:
    """Per-habit totals since `since`. Each log is converted to the habit's
    default unit when possible; logs in unrelated units get their own row."""
    rows = db.execute(
        select(Habit, DailyLog.amount, DailyLog.metric, DailyLog.log_date)
        .join(DailyLog, DailyLog.habit_id == Habit.habit_id)
        .where(DailyLog.log_date >= since, _live())
    ).all()
    groups: dict[tuple[int, str], dict] = {}
    for habit, amount, unit, log_date in rows:
        unit = resolve(unit, habit.metric)  # old "m" rows: meters for runs, minutes else
        target = unit
        converted = float(amount)
        if habit.metric:
            as_default = convert(float(amount), unit, habit.metric)
            if as_default is not None:
                target, converted = habit.metric, as_default
        group = groups.setdefault(
            (habit.habit_id, target),
            {"habit_id": habit.habit_id, "habit": habit.display_name, "metric": target,
             "total": 0.0, "dates": set()},
        )
        group["total"] += converted
        group["dates"].add(log_date)

    streaks = {hid: streak_days(db, hid) for hid, _ in groups}
    result = [
        {
            "habit": g["habit"],
            "metric": g["metric"],
            "total": round(g["total"], 2),
            "days": len(g["dates"]),
            "streak_days": streaks[g["habit_id"]],
        }
        for g in groups.values()
    ]
    return sorted(result, key=lambda r: (r["habit"], r["metric"]))


def reminder_check(db: Session, ref: Optional[date] = None) -> dict:
    """What an evening check-in should mention: streaks that end tonight
    unless logged, and recently-active habits not logged today."""
    ref = ref or today()
    at_risk, not_logged, done = [], [], []
    habits = db.execute(
        select(Habit).where(Habit.is_active).order_by(Habit.display_name)
    ).scalars()
    for habit in habits:
        days = _logged_days(db, habit.habit_id, ref)
        if ref in days:
            done.append(habit.display_name)
            continue
        if not any(d >= ref - timedelta(days=RECENT_DAYS) for d in days):
            continue  # dormant habit: don't nag
        streak = streak_days(db, habit.habit_id, ref)
        if streak >= 1:
            at_risk.append({"habit": habit.display_name, "streak_days": streak})
        else:
            not_logged.append(habit.display_name)
    at_risk.sort(key=lambda r: -r["streak_days"])
    return {"date": ref.isoformat(), "at_risk": at_risk, "not_logged": not_logged, "done": done}
