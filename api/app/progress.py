"""Streaks and running totals, shown after each log. Voided logs never count."""

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DailyLog, Habit
from app.timeutil import friendly_date, today


def _live():
    return DailyLog.voided_at.is_(None)


def streak_days(db: Session, habit_id: int, ref: Optional[date] = None) -> int:
    """Consecutive days with a log, ending today, or yesterday if today isn't logged yet."""
    ref = ref or today()
    days = set(
        db.execute(
            select(DailyLog.log_date)
            .where(DailyLog.habit_id == habit_id, _live(), DailyLog.log_date <= ref)
            .distinct()
        ).scalars()
    )
    day = ref if ref in days else ref - timedelta(days=1)
    streak = 0
    while day in days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def week_total(
    db: Session, habit_id: int, metric: str, ref: Optional[date] = None
) -> float:
    """Sum of this ISO week's (Mon to ref) logs for the habit in one unit."""
    ref = ref or today()
    monday = ref - timedelta(days=ref.weekday())
    total = db.execute(
        select(func.coalesce(func.sum(DailyLog.amount), 0)).where(
            DailyLog.habit_id == habit_id,
            DailyLog.metric == metric,
            DailyLog.log_date.between(monday, ref),
            _live(),
        )
    ).scalar()
    return float(total)


def log_summary(db: Session, log: DailyLog, habit: Habit) -> dict:
    return {
        "log_id": log.log_id,
        "habit": habit.display_name,
        "amount": float(log.amount),
        "metric": log.metric,
        "log_date": log.log_date.isoformat()
        if isinstance(log.log_date, date)
        else str(log.log_date),
        "when": friendly_date(log.log_date),
        "streak_days": streak_days(db, habit.habit_id),
        "week_total": week_total(db, habit.habit_id, log.metric),
    }
