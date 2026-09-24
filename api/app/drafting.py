"""
Turning a message into a validated draft.

Loop 1 (run_loop1): new message -> LLM intent -> validate -> audit row.
Loop 2 (regenerate_from_feedback): correction to an open draft -> same.
Both retry with the validation error, and fall back to the regex parser
when the LLM is unreachable. Both end in _apply_valid_intent, which decides
between "pending approval", "needs a unit" and "new habit?".
"""

import math
import re
from datetime import date
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app import llm_client
from app.models import AuditLog, Habit
from app.parser import parse_correction, parse_text
from app.sqlguard import bind_nulls, check_draft_sql
from app.timeutil import friendly_date, today
from app.units import canonical, quantity, resolve

MAX_EXTRA_LOGS = 5

# Keys of the LLM intent contract (llm/semantics.yaml: intent_shape).
INTENT_KEYS = (
    "habit_name", "proposed_habit", "amount", "metric",
    "suggested_metric", "log_date", "confidence", "draft_sql",
)
EXTRA_KEYS = ("habit_name", "amount", "metric", "log_date")


class FeedbackFailed(Exception):
    """A correction couldn't be applied; the draft is left as it was."""


def habits_context(db: Session) -> list[dict]:
    habits = db.execute(select(Habit).where(Habit.is_active)).scalars().all()
    recent_rows = db.execute(
        text(
            """
            SELECT DISTINCT ON (habit_id) habit_id, metric
            FROM daily_logs
            WHERE voided_at IS NULL
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


def llm_view(intent: Optional[dict]) -> Optional[dict]:
    """An audit's intent without our bookkeeping keys, to show the LLM its draft."""
    if not intent:
        return None
    view = {k: intent.get(k) for k in INTENT_KEYS}
    view["extra_logs"] = [
        {k: e.get(k) for k in EXTRA_KEYS} for e in intent.get("extra_logs") or []
    ]
    return view


def call_llm(**kwargs) -> dict:
    return llm_client.post("/extract", kwargs)["intent"]


def template_sql(habit_names: list[str]) -> str:
    """The draft SQL the LLM would write, for drafts made without it."""
    rows = []
    for i, name in enumerate(habit_names):
        suffix = "" if i == 0 else f"_{i + 1}"
        rows.append(
            f"((SELECT habit_id FROM habits WHERE name = '{name}'),"
            f" :amount{suffix}, :metric{suffix}, :log_date{suffix}, 'llm')"
        )
    return (
        "INSERT INTO daily_logs (habit_id, amount, metric, log_date, source)\nVALUES "
        + ",\n       ".join(rows)
    )


def _dry_run_sql(db: Session, sql: str) -> Optional[str]:
    if not sql or not isinstance(sql, str):
        return "draft_sql is missing"

    shape_error = check_draft_sql(sql)
    if shape_error:
        return shape_error

    try:
        # Savepoint: a failing EXPLAIN must not abort the caller's transaction.
        with db.begin_nested():
            db.execute(text(f"EXPLAIN {bind_nulls(sql)}"))
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
    return d <= today()


def _normalize_metric(m) -> Optional[str]:
    return canonical(m)


def _positive_amount(value) -> Optional[float]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount > 0 else None


def _slug(name) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")[:50]


def _find_habit(db: Session, name) -> Optional[Habit]:
    if not name:
        return None
    return db.execute(select(Habit).where(Habit.name == _slug(name))).scalar_one_or_none()


def habit_display_name(name: str) -> str:
    return name.replace("_", " ").title()


def _resolve_extras(intent: dict, db: Session) -> tuple[list[dict], list[str]]:
    """Validate extra_logs; unusable ones are skipped with a reason, not fatal."""
    raw = intent.get("extra_logs")
    if not isinstance(raw, list):
        raw = []
    resolved, skipped = [], []
    for item in raw[:MAX_EXTRA_LOGS]:
        if not isinstance(item, dict):
            continue
        label = item.get("habit_name") or item.get("proposed_habit") or "one item"
        habit = _find_habit(db, item.get("habit_name"))
        if habit is None or not habit.is_active:
            skipped.append(f"{habit_display_name(_slug(label))} (new habit, send it on its own)")
            continue
        amount = _positive_amount(item.get("amount"))
        if amount is None:
            skipped.append(f"{habit.display_name} (no amount)")
            continue
        log_date = item.get("log_date") or intent["log_date"]
        if not _valid_date(log_date):
            skipped.append(f"{habit.display_name} (date isn't valid)")
            continue
        metric, source = resolve(item.get("metric"), habit.metric), "explicit"
        if not metric:
            metric = resolve(item.get("suggested_metric"), habit.metric) or habit.metric
            source = "suggested"
        if not metric:
            skipped.append(f"{habit.display_name} (needs a unit, send it on its own)")
            continue
        resolved.append({
            "habit_id": habit.habit_id,
            "habit_name": habit.name,
            "display_name": habit.display_name,
            "amount": amount,
            "metric": metric,
            "metric_source": source,
            "log_date": log_date,
        })
    return resolved, skipped


def _validate(intent: dict, db: Session) -> tuple[bool, Optional[str], Optional[Habit]]:
    """Check (and normalize, in place) an LLM intent. Returns (ok, error, habit);
    habit is None when the intent proposes a new habit."""
    habit_name = intent.get("habit_name")
    proposed = intent.get("proposed_habit")

    if not habit_name and not proposed:
        return False, "Could not match a habit or propose a new one.", None

    amount = _positive_amount(intent.get("amount"))
    if amount is None:
        return False, f"Amount must be a positive number, got {intent.get('amount')!r}.", None
    intent["amount"] = amount

    if not _valid_date(intent.get("log_date")):
        return False, f"Invalid date: {intent.get('log_date')}", None

    intent["metric"] = _normalize_metric(intent.get("metric"))
    intent["suggested_metric"] = _normalize_metric(intent.get("suggested_metric"))

    sql_error = _dry_run_sql(db, intent.get("draft_sql") or "")
    if sql_error:
        return False, f"draft_sql failed: {sql_error}", None

    habit = _find_habit(db, habit_name)
    if habit is None:
        # Unknown habit_name is treated as a proposal: offering to create it
        # beats another LLM round trip that tells the model it was wrong.
        name = _slug(proposed or habit_name)
        if not name:
            return False, "Proposed habit name must contain letters or digits.", None
        habit = _find_habit(db, name)
        if habit is None:
            intent["habit_name"] = None
            intent["proposed_habit"] = name
            intent["extra_logs"], intent["skipped"] = _resolve_extras(intent, db)
            return True, None, None

    if not habit.is_active:
        return False, f"Habit '{habit.name}' is inactive.", None
    intent["metric"] = resolve(intent["metric"], habit.metric)
    intent["suggested_metric"] = resolve(intent["suggested_metric"], habit.metric)
    intent["habit_name"] = habit.name
    intent["proposed_habit"] = None
    intent["extra_logs"], intent["skipped"] = _resolve_extras(intent, db)
    return True, None, habit


def supersede_pending(chat_id: int, db: Session, keep_audit_id: Optional[int] = None):
    """Enforce one pending draft per chat (uq_audit_pending_per_chat).
    Call before moving a row to 'pending'; the caller commits."""
    stmt = update(AuditLog).where(
        AuditLog.chat_id == chat_id, AuditLog.status == "pending"
    )
    if keep_audit_id is not None:
        stmt = stmt.where(AuditLog.audit_id != keep_audit_id)
    db.execute(stmt.values(status="superseded"))


def _more_suffix(intent: dict) -> str:
    n = len(intent.get("extra_logs") or [])
    return f" (+{n} more log{'s' if n != 1 else ''} in this message)" if n else ""


def proposal_prompt(intent: dict, metric: Optional[str]) -> str:
    what = quantity(intent["amount"], metric) if metric else f"{intent['amount']:g}"
    when = friendly_date(intent["log_date"])
    follow_up = "" if metric else " I'll ask for the unit next."
    return (
        f"“{habit_display_name(intent['proposed_habit'])}” is a new habit. "
        f"Create it and log {what} {when}?{follow_up}{_more_suffix(intent)}"
    )


def unit_prompt(intent: dict, habit: Habit) -> str:
    example = habit.metric or "minutes, pages, km"
    return (
        f"Got it: {intent['amount']:g} of {habit.display_name} "
        f"{friendly_date(intent['log_date'])}. What unit? (e.g. {example})"
        f"{_more_suffix(intent)}"
    )


def offline_intent(user_text: str, habits: list[dict]) -> Optional[dict]:
    """Regex fallback for when the LLM is unreachable. Known habits only."""
    parsed = parse_text(user_text)
    if parsed is None:
        return None
    default = next(
        (h["default_metric"] for h in habits if h["name"] == parsed.habit_name), None
    )
    return {
        "habit_name": parsed.habit_name,
        "proposed_habit": None,
        "amount": float(parsed.amount),
        "metric": parsed.metric,
        "suggested_metric": None if parsed.metric else default,
        "log_date": parsed.log_date,
        "confidence": parsed.confidence,
        "draft_sql": template_sql([parsed.habit_name]),
        "extra_logs": [],
        "parser": "offline",
    }


def offline_revision(current: dict, feedback: str, habits: list[dict]) -> Optional[dict]:
    """Apply a correction without the LLM: "6 km, not 4", "it was yesterday",
    "reading, not running". None if the correction names nothing we can parse."""
    fields = parse_correction(feedback)
    if not fields:
        return None
    revised = llm_view(current)
    new_habit = fields.get("habit_name")
    if new_habit and new_habit != revised.get("habit_name"):
        revised["habit_name"], revised["proposed_habit"] = new_habit, None
        if "metric" not in fields:  # the old unit belonged to the old habit
            revised["metric"] = None
            revised["suggested_metric"] = next(
                (h["default_metric"] for h in habits if h["name"] == new_habit), None
            )
    for key in ("amount", "metric", "log_date"):
        if key in fields:
            revised[key] = fields[key]
    if "metric" in fields:
        revised["suggested_metric"] = None
    names = [revised.get("habit_name") or revised.get("proposed_habit")]
    names += [e["habit_name"] for e in revised["extra_logs"]]
    revised["draft_sql"] = template_sql(names)
    revised["parser"] = "offline"
    return revised


def _resolve_metric(intent: dict) -> tuple[Optional[str], str]:
    if intent.get("metric"):
        return intent["metric"], "explicit"
    if intent.get("suggested_metric"):
        return intent["suggested_metric"], "suggested"
    return None, "missing"


def _apply_valid_intent(
    audit: AuditLog, intent: dict, habit: Optional[Habit], attempts: int, db: Session
) -> tuple[Optional[str], Optional[str]]:
    """Put a validated intent on the audit row and pick the next step.
    Returns (clarify_prompt, proposal_prompt); both None means pending approval.
    The caller commits."""
    metric, source = _resolve_metric(intent)
    new_intent = {**intent, "metric": metric, "metric_source": source, "attempts": attempts}
    audit.draft_sql = intent.get("draft_sql")
    audit.iteration_count = attempts
    audit.error_message = None

    if habit is None:
        audit.intent = {**new_intent, "needs": "habit_approval"}
        audit.status = "awaiting_input"
        return None, proposal_prompt(intent, metric)

    new_intent["habit_id"] = habit.habit_id
    if metric is None:
        audit.intent = {**new_intent, "needs": "unit"}
        audit.status = "awaiting_input"
        return unit_prompt(intent, habit), None

    supersede_pending(audit.chat_id, db, keep_audit_id=audit.audit_id)
    audit.intent = new_intent
    audit.status = "pending"
    return None, None


def run_loop1(
    user_text: str,
    chat_id: int,
    db: Session,
    max_retries: int = 3,
) -> tuple[AuditLog, Optional[str], Optional[str]]:
    supersede_pending(chat_id, db)
    db.commit()

    habits = habits_context(db)
    intent: Optional[dict] = None
    error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            intent = call_llm(
                user_text=user_text,
                habits=habits,
                previous_intent=intent,
                previous_error=error,
            )
        except Exception as e:
            error = f"LLM call failed: {e}"
            intent = offline_intent(user_text, habits)
            if intent is None:
                continue

        is_valid, validation_error, habit = _validate(intent, db)

        if is_valid:
            audit = AuditLog(chat_id=chat_id, user_input=user_text)
            clarify, proposal = _apply_valid_intent(audit, intent, habit, attempt, db)
            db.add(audit)
            db.commit()
            db.refresh(audit)
            return audit, clarify, proposal

        error = validation_error
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
    Loop 2: regenerate an open draft from the user's correction.
    Returns (audit, clarify_prompt, proposal_prompt), same shape as run_loop1.
    Raises FeedbackFailed if no attempt validates; the draft itself is left
    untouched so it can still be approved, discarded or corrected again.
    """
    habits = habits_context(db)
    current = dict(audit.intent or {})
    history = [*(current.get("feedback_history") or []), feedback]
    last_intent: Optional[dict] = None
    error: Optional[str] = None
    base_iteration = audit.iteration_count or 0

    for attempt in range(1, max_retries + 1):
        kwargs = {
            "original_text": audit.user_input,
            "clarification": feedback,
            "current_draft": llm_view(current),
            "habits": habits,
        }
        if last_intent is not None:
            kwargs["previous_intent"] = last_intent
            kwargs["previous_error"] = error
        try:
            intent = call_llm(**kwargs)
        except Exception as e:
            error = f"LLM call failed: {e}"
            intent = offline_revision(current, feedback, habits)
            if intent is None:
                continue

        is_valid, validation_error, habit = _validate(intent, db)
        if is_valid:
            clarify, proposal = _apply_valid_intent(
                audit, intent, habit, base_iteration + attempt, db
            )
            audit.intent = {**audit.intent, "feedback_history": history}
            audit.user_feedback = feedback
            db.commit()
            db.refresh(audit)
            return audit, clarify, proposal

        error = validation_error
        last_intent = intent

    audit.intent = {**current, "feedback_history": history}
    audit.user_feedback = feedback
    audit.iteration_count = base_iteration + max_retries
    audit.error_message = f"Loop 2 failed after {max_retries} attempts. Last: {error}"
    db.commit()
    db.refresh(audit)
    raise FeedbackFailed(audit.error_message)
