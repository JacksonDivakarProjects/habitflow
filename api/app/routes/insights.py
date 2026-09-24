"""Reading the data back: habits, today, stats."""

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DailyLog, Habit
from app.progress import habit_stats
from app.timeutil import today

router = APIRouter(prefix="/internal", tags=["insights"])


@router.get("/habits")
def list_habits(db: Session = Depends(get_db)):
    rows = (
        db.execute(select(Habit).where(Habit.is_active).order_by(Habit.display_name))
        .scalars()
        .all()
    )
    return [
        {
            "habit_id": h.habit_id,
            "name": h.name,
            "display_name": h.display_name,
            "metric": h.metric,
        }
        for h in rows
    ]



@router.get("/today")
def today_logs(db: Session = Depends(get_db)):
    day = today()
    rows = db.execute(
        select(DailyLog, Habit.display_name)
        .join(Habit, Habit.habit_id == DailyLog.habit_id)
        .where(DailyLog.log_date == day, DailyLog.voided_at.is_(None))
        .order_by(DailyLog.log_id)
    ).all()
    return {
        "date": day.isoformat(),
        "logs": [
            {
                "log_id": log.log_id,
                "habit": name,
                "amount": float(log.amount),
                "metric": log.metric,
            }
            for log, name in rows
        ],
    }


@router.get("/stats")
def stats(chat_id: int, db: Session = Depends(get_db)):
    return habit_stats(db, since=today() - timedelta(days=29))  # 30 days incl. today
