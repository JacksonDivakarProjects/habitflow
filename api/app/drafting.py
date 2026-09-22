from datetime import date
from typing import Optional

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, Habit


def _habits_context(db: Session) -> list[dict]:
    habits = db.execute(select(Habit).where(Habit.is_active)).scalars().all()
    recent_rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (habit_id) habit_id, metric
            FROM daily_logs
            ORDER BY habit_id, log_date DESC, log_id DESC
            """
        )
    ).all()
    recent = {r.habit_id: r.metric for r in recent_rows}
    return [
        {
            "name": h.name,
            "display_name": h.display_name,
            "default_metric": h.metric,
            "recent_metric": recent.get(h.habit_id),
        }
        for h in habits
    ]


def _call_llm(**kwargs) -> dict:
    r = httpx.post(f"{settings.llm_base_url}/extract", json=kwargs, timeout=30)
    r.raise_for_status()
    return r.json()["intent"]


def _dry_run_sql(db: Session, sql: str) -> Optional[str]:
    if not sql or not isinstance(sql, str):
        return "draft_sql is missing"

    forbidden = ("drop", "delete", "update", "truncate", "alter", "grant")
    lowered = sql.lower()
    for word in forbidden:
        if f" {word} " in f" {lowered} ":
            return f"forbidden operation: {word}"

    import re
    test_sql = re.sub(r":[a-zA-Z_][a-zA-Z0-9_]*", "NULL", sql)

    try:
        db.execute(text(f"EXPLAIN {test_sql}"))
        return None
    except Exception as e:
        return str(e).split("\n")[0]


def _valid_date(s: Optional[str]) -> bool:
    if not s or s in ("TODAY", "YESTERDAY"):
        return False
    try:
        d = date.fromisoformat(s)
    except (ValueError, TypeError):
        return False
    return d <= date.today()


def _normalize_metric(m) -> Optional[str]:
    if m is None:
        return None
    s = str(m).strip().lower()
    return s or None


def _validate(intent: dict, db: Session) -> tuple[bool, Optional[str], Optional[Habit]]:
    habit_name = intent.get("habit_name")
    proposed = intent.get("proposed_habit")

    if not habit_name and not proposed:
        return False, "Could not match a habit or propose a new one.", None

    amount = intent.get("amount")
    if amount is None or float(amount) <= 0:
        return False, "Amount must be a positive number.", None

    if not _valid_date(intent.get("log_date")):
        return False, f"Invalid date: {intent.get('log_date')}", None

    intent["metric"] = _normalize_metric(intent.get("metric"))
    intent["suggested_metric"] = _normalize_metric(intent.get("suggested_metric"))

    sql_error = _dry_run_sql(db, intent.get("draft_sql") or "")
    if sql_error:
        return False, f"draft_sql failed: {sql_error}", None

    if not habit_name and proposed:
        name = str(proposed).strip().lower().replace(" ", "_")
        if not name.replace("_", "").isalnum():
            return False, "Proposed habit name must be alphanumeric or snake_case.", None
        existing = db.execute(
            select(Habit).where(Habit.name == name)
        ).scalar_one_or_none()
        if existing:
            intent["habit_name"] = existing.name
            intent["proposed_habit"] = None
            return True, None, existing
        intent["proposed_habit"] = name
        return True, None, None

    habit = db.execute(
        select(Habit).where(Habit.name == habit_name)
    ).scalar_one_or_none()
    if not habit or not habit.is_active:
        return False, f"Habit '{habit_name}' is not known or is inactive.", None

    return True, None, habit


def _supersede_pending(chat_id: int, db: Session):
    db.execute(
        update(AuditLog)
        .where(AuditLog.chat_id == chat_id, AuditLog.status == "pending")
        .values(status="superseded")
    )
    db.commit()


def _resolve_metric(intent: dict) -> tuple[Optional[str], str]:
    if intent.get("metric"):
        return intent["metric"], "explicit"
    if intent.get("suggested_metric"):
        return intent["suggested_metric"], "suggested"
    return None, "missing"


def run_loop1(
    user_text: str,
    chat_id: int,
    db: Session,
    max_retries: int = 3,
) -> tuple[AuditLog, Optional[str], Optional[str]]:
    _supersede_pending(chat_id, db)

    habits = _habits_context(db)
    intent: Optional[dict] = None
    error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            intent = _call_llm(
                user_text=user_text,
                habits=habits,
                previous_intent=intent,
                previous_error=error,
            )
        except Exception as e:
            error = f"LLM call failed: {e}"
            intent = None
            continue

        is_valid, validation_error, habit = _validate(intent, db)

        if is_valid:
            if habit is None:
                metric, source = _resolve_metric(intent)
                audit = AuditLog(
                    chat_id=chat_id,
                    user_input=user_text,
                    intent={
                        **intent,
                        "metric": metric,
                        "metric_source": source,
                        "attempts": attempt,
                        "needs": "habit_approval",
                    },
                    status="awaiting_input",
                    draft_sql=intent.get("draft_sql"),
                    iteration_count=attempt,
                )
                db.add(audit)
                db.commit()
                db.refresh(audit)
                hint = metric or "no default"
                prompt = (
                    f"I don't know '{intent['proposed_habit']}' yet. "
                    f"Create it (suggested default: {hint}) and log "
                    f"{intent['amount']:g} {metric or ''}?"
                )
                return audit, None, prompt

            metric, source = _resolve_metric(intent)

            if metric is None:
                audit = AuditLog(
                    chat_id=chat_id,
                    user_input=user_text,
                    intent={
                        **intent,
                        "habit_id": habit.habit_id,
                        "attempts": attempt,
                        "needs": "unit",
                    },
                    status="awaiting_input",
                    draft_sql=intent.get("draft_sql"),
                    iteration_count=attempt,
                )
                db.add(audit)
                db.commit()
                db.refresh(audit)
                unit_hint = habit.metric or "a unit"
                prompt = (
                    f"Got it: {intent['amount']:g} of {habit.display_name} "
                    f"on {intent['log_date']}. What unit? (e.g. {unit_hint})"
                )
                return audit, prompt, None

            audit = AuditLog(
                chat_id=chat_id,
                user_input=user_text,
                intent={
                    **intent,
                    "habit_id": habit.habit_id,
                    "metric": metric,
                    "metric_source": source,
                    "attempts": attempt,
                },
                status="pending",
                draft_sql=intent.get("draft_sql"),
                iteration_count=attempt,
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
            return audit, None, None

        error = validation_error
        if intent is not None:
            db.add(AuditLog(
                chat_id=chat_id,
                user_input=user_text,
                intent={**intent, "attempts": attempt, "validation_error": error},
                status="failed",
                draft_sql=intent.get("draft_sql"),
                error_message=error,
                iteration_count=attempt,
            ))
            db.commit()

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
    return audit, None, None


def regenerate_from_feedback(
    audit: AuditLog,
    feedback: str,
    db: Session,
    max_retries: int = 3,
) -> tuple[AuditLog, Optional[str], Optional[str]]:
    """
    Loop 2: regenerate the intent for an existing audit row based on user feedback.
    Returns (audit, clarify_prompt, proposal_prompt), same shape as run_loop1.
    """
    habits = _habits_context(db)
    intent: Optional[dict] = None
    last_intent: Optional[dict] = None
    error: Optional[str] = None
    base_iteration = audit.iteration_count or 0

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = {
                "original_text": audit.user_input,
                "clarification": feedback,
                "habits": habits,
            }
            if attempt > 1 and last_intent:
                kwargs["previous_intent"] = last_intent
                kwargs["previous_error"] = error

            intent = _call_llm(**kwargs)
        except Exception as e:
            error = f"LLM call failed: {e}"
            continue

        is_valid, validation_error, habit = _validate(intent, db)

        if is_valid:
            # New habit proposal
            if habit is None:
                metric, source = _resolve_metric(intent)
                audit.intent = {
                    **intent,
                    "metric": metric,
                    "metric_source": source,
                    "attempts": base_iteration + attempt,
                    "needs": "habit_approval",
                }
                audit.draft_sql = intent.get("draft_sql")
                audit.user_feedback = feedback
                audit.iteration_count = base_iteration + attempt
                audit.status = "awaiting_input"
                audit.error_message = None
                db.commit()
                db.refresh(audit)
                hint = metric or "no default"
                prompt = (
                    f"I don't know '{intent['proposed_habit']}' yet. "
                    f"Create it (suggested default: {hint}) and log "
                    f"{intent['amount']:g} {metric or ''}?"
                )
                return audit, None, prompt

            metric, source = _resolve_metric(intent)

            if metric is None:
                audit.intent = {
                    **intent,
                    "habit_id": habit.habit_id,
                    "attempts": base_iteration + attempt,
                    "needs": "unit",
                }
                audit.draft_sql = intent.get("draft_sql")
                audit.user_feedback = feedback
                audit.iteration_count = base_iteration + attempt
                audit.status = "awaiting_input"
                audit.error_message = None
                db.commit()
                db.refresh(audit)
                unit_hint = habit.metric or "a unit"
                prompt = (
                    f"Got it: {intent['amount']:g} of {habit.display_name} "
                    f"on {intent['log_date']}. What unit? (e.g. {unit_hint})"
                )
                return audit, prompt, None

            audit.intent = {
                **intent,
                "habit_id": habit.habit_id,
                "metric": metric,
                "metric_source": source,
                "attempts": base_iteration + attempt,
            }
            audit.draft_sql = intent.get("draft_sql")
            audit.user_feedback = feedback
            audit.iteration_count = base_iteration + attempt
            audit.status = "pending"
            audit.error_message = None
            db.commit()
            db.refresh(audit)
            return audit, None, None

        # Failed validation — retry with the error message
        error = validation_error
        last_intent = intent

    # All retries failed
    audit.status = "failed"
    audit.error_message = f"Loop 2 failed after {max_retries} attempts. Last: {error}"
    audit.user_feedback = feedback
    audit.iteration_count = base_iteration + max_retries
    db.commit()
    db.refresh(audit)
    return audit, None, None