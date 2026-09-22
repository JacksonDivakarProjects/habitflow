from datetime import date
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, Habit


def _validate_intent(
    intent: dict, habits: list[Habit]
) -> tuple[bool, Optional[str], Optional[Habit]]:
    habit_name = intent.get("habit_name")
    if not habit_name:
        return False, "Could not identify a known habit.", None

    habit = next((h for h in habits if h.name == habit_name), None)
    if not habit:
        return False, f"Habit '{habit_name}' is not a known, active habit.", None

    amount = intent.get("amount")
    if amount is None or float(amount) <= 0:
        return False, "Amount must be a positive number.", None

    log_date = intent.get("log_date")
    if not log_date or log_date in ("TODAY", "YESTERDAY"):
        return False, f"log_date must be an explicit YYYY-MM-DD, got '{log_date}'.", None

    try:
        parsed = date.fromisoformat(log_date)
    except ValueError:
        return False, f"Invalid date format: '{log_date}'. Use YYYY-MM-DD.", None

    if parsed > date.today():
        return False, "Log date cannot be in the future.", None

    return True, None, habit


def _call_llm(
    user_text: str,
    habits_context: list[dict],
    previous_intent: Optional[dict] = None,
    previous_error: Optional[str] = None,
) -> dict:
    r = httpx.post(
        f"{settings.llm_base_url}/extract",
        json={
            "user_text": user_text,
            "habits": habits_context,
            "previous_intent": previous_intent,
            "previous_error": previous_error,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["intent"]


def run_loop1(
    user_text: str,
    chat_id: int,
    db: Session,
    max_retries: int = 3,
) -> tuple[AuditLog, Optional[str]]:
    habits = db.execute(select(Habit).where(Habit.is_active)).scalars().all()
    habits_context = [
        {"name": h.name, "metric": h.metric, "display_name": h.display_name}
        for h in habits
    ]

    intent: Optional[dict] = None
    error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            intent = _call_llm(
                user_text,
                habits_context,
                previous_intent=intent,
                previous_error=error,
            )
        except Exception as e:
            error = f"LLM service call failed: {e}"
            intent = None
            continue

        is_valid, validation_error, habit = _validate_intent(intent, habits)

        if is_valid:
            enriched_intent = {
                **intent,
                "habit_id": habit.habit_id,
                "attempts": attempt,
            }

            if intent.get("metric") is None:
                audit = AuditLog(
                    chat_id=chat_id,
                    user_input=user_text,
                    intent=enriched_intent,
                    status="awaiting_input",
                    iteration_count=attempt,
                )
                db.add(audit)
                db.commit()
                db.refresh(audit)
                prompt = (
                    f"Got it: {intent['amount']:g} of {habit.display_name} "
                    f"on {intent['log_date']}. What unit? (e.g. {habit.metric})"
                )
                return audit, prompt

            audit = AuditLog(
                chat_id=chat_id,
                user_input=user_text,
                intent=enriched_intent,
                status="pending",
                iteration_count=attempt,
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
            return audit, None

        # Failed validation. Log it and retry.
        error = validation_error
        if intent is not None:
            db.add(
                AuditLog(
                    chat_id=chat_id,
                    user_input=user_text,
                    intent={**intent, "attempts": attempt, "validation_error": error},
                    status="failed",
                    error_message=error,
                    iteration_count=attempt,
                )
            )
            db.commit()

    # All retries exhausted
    audit = AuditLog(
        chat_id=chat_id,
        user_input=user_text,
        status="failed",
        error_message=f"Loop 1 failed after {max_retries} attempts. Last: {error}",
        iteration_count=max_retries,
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)
    return audit, None