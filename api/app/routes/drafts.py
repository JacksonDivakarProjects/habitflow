"""Logging: a message becomes a draft, gets fixed up, then approved or discarded.

draft -> (clarify unit | approve_habit) -> execute -> undo, plus feedback
(corrections) and discard at any open stage. Each chat has at most one
pending draft (uq_audit_pending_per_chat).
"""

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import drafting
from app.db import get_db
from app.drafting import FeedbackFailed, regenerate_from_feedback, run_loop1
from app.models import AuditLog, DailyLog, Habit
from app.parser import find_habit, find_unit
from app.progress import log_summary
from app.timeutil import friendly_date
from app.units import canonical, family_units, quantity, resolve

router = APIRouter(prefix="/internal", tags=["logging"])

OPEN_STATUSES = ("pending", "awaiting_input")


def _record_duration(audit: AuditLog, started: float, db: Session):
    """Store how long drafting took (all LLM attempts) on the audit row."""
    audit.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    db.refresh(audit)


def _lock_audit(db: Session, audit_id: int) -> AuditLog:
    audit = db.execute(
        select(AuditLog).where(AuditLog.audit_id == audit_id).with_for_update()
    ).scalar_one_or_none()
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    return audit


def _status_conflict(audit: AuditLog) -> HTTPException:
    return HTTPException(status_code=409, detail=f"Audit status is {audit.status}")


def _preview(amount, metric, display_name: str, log_date) -> str:
    return f"{quantity(amount, metric)} of {display_name} {friendly_date(log_date)}"


# ------------------------------------------------------------------
# /draft
# ------------------------------------------------------------------
class DraftRequest(BaseModel):
    chat_id: int
    text: str
    message_id: int | None = None


class DraftResponse(BaseModel):
    audit_id: int
    preview: str | None = None
    intent: dict | None = None
    draft_sql: str | None = None
    needs_input: str | None = None
    prompt: str | None = None
    metric_source: str | None = None
    offline: bool = False  # drafted by the regex fallback, LLM unavailable
    skipped: list[str] = []  # parts of the message that weren't included
    items: list[dict] = []  # one per log: habit, amount, unit, log_date, when
    unit_options: list[str] = []  # quick answers for needs_input="unit"
    status: str | None = None


UNIT_OPTIONS = ["minutes", "hours", "reps", "pages", "km"]


def unit_options(db: Session, audit: AuditLog) -> list[str]:
    """Quick answers to "what unit?": the habit's own unit family first."""
    habit_id = (audit.intent or {}).get("habit_id")
    habit = db.get(Habit, habit_id) if habit_id else None
    options = family_units(habit.metric) if habit else []
    return (options + [u for u in UNIT_OPTIONS if u not in options])[:5]


def _item(amount, metric, display_name: str, log_date) -> dict:
    return {
        "habit": display_name,
        "amount": float(amount),
        "unit": metric,
        "quantity": quantity(amount, metric),
        "log_date": str(log_date),
        "when": friendly_date(log_date),
    }


def _card(audit: AuditLog, db: Session) -> DraftResponse:
    """Response for an audit that is pending approval."""
    intent = audit.intent
    habit = db.get(Habit, intent["habit_id"])
    entries = [(intent["amount"], intent["metric"], habit.display_name, intent["log_date"])]
    entries += [
        (e["amount"], e["metric"], e["display_name"], e["log_date"])
        for e in intent.get("extra_logs") or []
    ]
    return DraftResponse(
        audit_id=audit.audit_id,
        status=audit.status,
        preview="\n".join(_preview(*e) for e in entries),
        items=[_item(*e) for e in entries],
        intent=intent,
        draft_sql=audit.draft_sql,
        metric_source=intent.get("metric_source", "explicit"),
        offline=intent.get("parser") == "offline",
        skipped=intent.get("skipped") or [],
    )


def _loop_response(audit: AuditLog, clarify_prompt, proposal_prompt, db: Session) -> DraftResponse:
    skipped = (audit.intent or {}).get("skipped") or []
    if clarify_prompt:
        return DraftResponse(
            audit_id=audit.audit_id, needs_input="unit", prompt=clarify_prompt,
            draft_sql=audit.draft_sql, skipped=skipped, unit_options=unit_options(db, audit),
            status=audit.status,
        )
    if proposal_prompt:
        return DraftResponse(
            audit_id=audit.audit_id, needs_input="habit", prompt=proposal_prompt,
            draft_sql=audit.draft_sql, skipped=skipped, status=audit.status,
        )
    return _card(audit, db)


class DraftFailed(Exception):
    """The message couldn't be turned into a log."""


def create_draft(req: DraftRequest, db: Session) -> DraftResponse:
    started = time.monotonic()
    audit, clarify_prompt, proposal_prompt = run_loop1(req.text, req.chat_id, db)
    audit.message_id = req.message_id
    _record_duration(audit, started, db)
    if audit.status == "failed":
        raise DraftFailed(audit.error_message or "Couldn't understand.")
    return _loop_response(audit, clarify_prompt, proposal_prompt, db)


@router.post("/draft", response_model=DraftResponse)
def draft(req: DraftRequest, db: Session = Depends(get_db)):
    try:
        return create_draft(req, db)
    except DraftFailed as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/drafts/{audit_id}")
def get_draft(audit_id: int, db: Session = Depends(get_db)):
    """A draft's SQL and state, for the bot's "SQL" button."""
    audit = db.get(AuditLog, audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    return {
        "audit_id": audit.audit_id,
        "status": audit.status,
        "user_input": audit.user_input,
        "draft_sql": audit.draft_sql,
        "final_sql": audit.final_sql,
        "intent": audit.intent,
    }


# ------------------------------------------------------------------
# /clarify — missing unit
# ------------------------------------------------------------------
class ClarifyRequest(BaseModel):
    audit_id: int
    value: str


def _drop_for_new_log(audit: AuditLog, db: Session):
    """The answer to a unit question was really a new log: cancel the question
    and tell the bot (409 new_log) to draft the text as a fresh message."""
    audit.status = "cancelled"
    audit.error_message = "Unit question dropped: the answer was a new log"
    db.commit()
    raise HTTPException(
        status_code=409, detail={"code": "new_log", "dropped": audit.user_input}
    )


@router.post("/clarify", response_model=DraftResponse)
def clarify(req: ClarifyRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)
    if audit.status != "awaiting_input":
        raise _status_conflict(audit)

    intent = dict(audit.intent or {})
    if intent.get("needs") != "unit":
        raise HTTPException(status_code=409, detail="Not a unit clarification")

    lowered = req.value.strip().lower()
    words = lowered.split()
    current = intent.get("habit_name")

    # An answer about a different habit ("read 20 pages" to "What unit?" for a
    # run) is a new log, not a unit: drop the question instead of mislabelling.
    mentioned = find_habit(lowered)
    if mentioned and mentioned != current:
        _drop_for_new_log(audit, db)

    quick_unit = find_unit(lowered) if len(words) <= 3 else None
    bare_word = words[0].strip(".!") if len(words) == 1 else None
    if quick_unit:
        new_intent, metric = {}, quick_unit  # "km", "in km", "Mins.": no LLM needed
    else:
        try:
            new_intent = drafting.call_llm(
                original_text=audit.user_input,
                clarification=req.value,
                current_draft=drafting.llm_view(intent),
                habits=drafting.habits_context(db),
            )
        except Exception:
            # LLM unavailable: a single word ("reps") is still usable as typed.
            new_intent = {}
            metric = bare_word if bare_word and bare_word.isalpha() else None
        else:
            other = new_intent.get("habit_name") or new_intent.get("proposed_habit")
            if other and drafting._slug(other) != current:
                _drop_for_new_log(audit, db)
            metric = new_intent.get("metric") or new_intent.get("suggested_metric")

    if not metric:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=f"Sorry, I didn't catch a unit in “{req.value}”. Try e.g. “km” or “minutes”.",
            draft_sql=audit.draft_sql,
            unit_options=unit_options(db, audit),
            status=audit.status,
        )

    habit = db.get(Habit, intent["habit_id"])
    intent["metric"] = resolve(metric, habit.metric if habit else None)
    intent["metric_source"] = "explicit"
    new_sql = new_intent.get("draft_sql")
    if new_sql and drafting._dry_run_sql(db, new_sql) is None:  # same guard as drafts
        intent["draft_sql"] = new_sql
    intent.pop("needs", None)

    drafting.supersede_pending(audit.chat_id, db, keep_audit_id=audit.audit_id)
    audit.intent = intent
    audit.draft_sql = intent.get("draft_sql") or audit.draft_sql
    audit.status = "pending"
    db.commit()
    db.refresh(audit)
    return _card(audit, db)


# ------------------------------------------------------------------
# /approve_habit — create a proposed habit
# ------------------------------------------------------------------
class ApproveHabitRequest(BaseModel):
    audit_id: int
    accept: bool


@router.post("/approve_habit", response_model=DraftResponse)
def approve_habit(req: ApproveHabitRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)
    if audit.status != "awaiting_input":
        raise _status_conflict(audit)

    intent = dict(audit.intent or {})
    if intent.get("needs") != "habit_approval":
        raise HTTPException(status_code=409, detail="Not a habit approval")

    if not req.accept:
        audit.status = "cancelled"
        db.commit()
        return DraftResponse(
            audit_id=audit.audit_id, preview="Cancelled.", draft_sql=audit.draft_sql
        )

    name = intent["proposed_habit"]
    default_metric = canonical(intent.get("metric") or intent.get("suggested_metric"))

    habit = db.execute(select(Habit).where(Habit.name == name)).scalar_one_or_none()
    if habit is None:
        habit = Habit(
            name=name,
            display_name=drafting.habit_display_name(name),
            metric=default_metric,
        )
        db.add(habit)
        db.flush()

    intent["habit_name"] = name
    intent["habit_id"] = habit.habit_id
    intent.pop("proposed_habit", None)
    intent.pop("needs", None)

    if default_metric is None:
        intent["needs"] = "unit"
        audit.intent = intent
        audit.status = "awaiting_input"
        db.commit()
        db.refresh(audit)
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=(
                f"Created {habit.display_name}. What unit is the "
                f"{intent['amount']:g} {friendly_date(intent['log_date'])}? "
                f"(e.g. reps, minutes, pages)"
            ),
            draft_sql=audit.draft_sql,
            unit_options=unit_options(db, audit),
            status=audit.status,
        )

    intent["metric"] = default_metric
    intent["metric_source"] = intent.get("metric_source") or "suggested"
    drafting.supersede_pending(audit.chat_id, db, keep_audit_id=audit.audit_id)
    audit.intent = intent
    audit.status = "pending"
    db.commit()
    db.refresh(audit)
    return _card(audit, db)


# ------------------------------------------------------------------
# /discard — drop an open draft
# ------------------------------------------------------------------
class AuditRequest(BaseModel):
    audit_id: int


@router.post("/discard")
def discard(req: AuditRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)
    if audit.status == "cancelled":
        return {"status": "already_discarded"}
    if audit.status not in OPEN_STATUSES:
        raise _status_conflict(audit)
    audit.status = "cancelled"
    db.commit()
    return {"status": "discarded"}


# ------------------------------------------------------------------
# /execute
# ------------------------------------------------------------------
FINAL_SQL = (
    "INSERT INTO daily_logs (habit_id, amount, metric, log_date, source, audit_id) "
    "VALUES (:habit_id, :amount, :metric, :log_date, 'llm', :audit_id)"
)


@router.post("/execute")
def execute(req: AuditRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)

    if audit.status == "executed":
        log_ids = db.execute(
            select(DailyLog.log_id)
            .where(DailyLog.audit_id == audit.audit_id)
            .order_by(DailyLog.log_id)
        ).scalars().all()
        return {
            "status": "already_executed",
            "log_id": log_ids[0] if log_ids else None,
            "log_ids": log_ids,
            "summary": None,
            "summaries": [],
        }
    if audit.status != "pending":
        raise _status_conflict(audit)

    intent = audit.intent or {}
    main = {k: intent.get(k) for k in ("habit_id", "amount", "metric", "log_date")}
    if not all(main.values()):
        audit.status = "failed"
        audit.error_message = "Incomplete intent"
        db.commit()
        raise HTTPException(status_code=422, detail="Intent incomplete")

    entries = [main] + [
        {k: e[k] for k in ("habit_id", "amount", "metric", "log_date")}
        for e in intent.get("extra_logs") or []
    ]
    logs = []
    for entry in entries:
        log = DailyLog(
            **entry, source="llm", raw_input=audit.user_input, audit_id=audit.audit_id
        )
        db.add(log)
        logs.append(log)
    db.flush()

    audit.status = "executed"
    audit.final_sql = FINAL_SQL + (f"  -- x{len(logs)} rows" if len(logs) > 1 else "")
    db.commit()

    summaries = []
    for log in logs:
        db.refresh(log)
        summaries.append(log_summary(db, log, db.get(Habit, log.habit_id)))
    return {
        "status": "executed",
        "log_id": logs[0].log_id,
        "log_ids": [log.log_id for log in logs],
        "summary": summaries[0],
        "summaries": summaries,
    }


# ------------------------------------------------------------------
# /undo — void logs (kept for audit, excluded everywhere else)
# ------------------------------------------------------------------
class UndoRequest(BaseModel):
    log_id: int | None = None    # one log
    audit_id: int | None = None  # every log from one approved draft
    # neither: the most recent live log


@router.post("/undo")
def undo(req: UndoRequest, db: Session = Depends(get_db)):
    query = select(DailyLog).with_for_update().order_by(DailyLog.log_id)
    if req.log_id is not None:
        query = query.where(DailyLog.log_id == req.log_id)
    elif req.audit_id is not None:
        query = query.where(DailyLog.audit_id == req.audit_id)
    else:
        query = (
            select(DailyLog).with_for_update()
            .where(DailyLog.voided_at.is_(None))
            .order_by(DailyLog.log_id.desc())
            .limit(1)
        )
    logs = db.execute(query).scalars().all()
    if not logs:
        raise HTTPException(status_code=404, detail="Nothing to undo")

    previews = []
    for log in logs:
        habit = db.get(Habit, log.habit_id)
        previews.append(_preview(log.amount, log.metric, habit.display_name, log.log_date))
    live = [log for log in logs if log.voided_at is None]
    for log in live:
        log.voided_at = func.now()
    db.commit()
    return {
        "status": "undone" if live else "already_undone",
        "log_id": logs[0].log_id,
        "log_ids": [log.log_id for log in logs],
        "preview": " and ".join(previews),
    }


# ------------------------------------------------------------------
# /feedback — Loop 2 (user correction)
# ------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    audit_id: int
    feedback: str


@router.post("/feedback", response_model=DraftResponse)
def feedback(req: FeedbackRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)
    if audit.status not in OPEN_STATUSES:
        raise _status_conflict(audit)

    started = time.monotonic()
    try:
        audit, clarify_prompt, proposal_prompt = regenerate_from_feedback(
            audit, req.feedback, db
        )
    except FeedbackFailed as e:
        _record_duration(audit, started, db)
        raise HTTPException(status_code=422, detail=str(e)) from e
    _record_duration(audit, started, db)
    return _loop_response(audit, clarify_prompt, proposal_prompt, db)


