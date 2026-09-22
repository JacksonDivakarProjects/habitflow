import re
import time
from datetime import time as dtime
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import drafting
from app.db import get_db
from app.drafting import FeedbackFailed, regenerate_from_feedback, run_loop1
from app.models import AuditLog, DailyLog, Habit, ReminderSetting
from app.progress import habit_stats, log_summary, reminder_check
from app.timeutil import friendly_date, today
from app.units import canonical, quantity
from app.units import is_known as is_known_unit

router = APIRouter(prefix="/internal", tags=["internal"])

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
# /habits
# ------------------------------------------------------------------
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


def _card(audit: AuditLog, db: Session) -> DraftResponse:
    """Response for an audit that is pending approval."""
    intent = audit.intent
    habit = db.get(Habit, intent["habit_id"])
    lines = [_preview(intent["amount"], intent["metric"], habit.display_name, intent["log_date"])]
    lines += [
        _preview(e["amount"], e["metric"], e["display_name"], e["log_date"])
        for e in intent.get("extra_logs") or []
    ]
    return DraftResponse(
        audit_id=audit.audit_id,
        preview="\n".join(lines),
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
            draft_sql=audit.draft_sql, skipped=skipped,
        )
    if proposal_prompt:
        return DraftResponse(
            audit_id=audit.audit_id, needs_input="habit", prompt=proposal_prompt,
            draft_sql=audit.draft_sql, skipped=skipped,
        )
    return _card(audit, db)


@router.post("/draft", response_model=DraftResponse)
def draft(req: DraftRequest, db: Session = Depends(get_db)):
    started = time.monotonic()
    audit, clarify_prompt, proposal_prompt = run_loop1(req.text, req.chat_id, db)
    audit.message_id = req.message_id
    _record_duration(audit, started, db)
    if audit.status == "failed":
        raise HTTPException(status_code=422, detail=audit.error_message or "Couldn't understand.")
    return _loop_response(audit, clarify_prompt, proposal_prompt, db)


# ------------------------------------------------------------------
# /clarify — missing unit
# ------------------------------------------------------------------
class ClarifyRequest(BaseModel):
    audit_id: int
    value: str


@router.post("/clarify", response_model=DraftResponse)
def clarify(req: ClarifyRequest, db: Session = Depends(get_db)):
    audit = _lock_audit(db, req.audit_id)
    if audit.status != "awaiting_input":
        raise _status_conflict(audit)

    intent = dict(audit.intent or {})
    if intent.get("needs") != "unit":
        raise HTTPException(status_code=409, detail="Not a unit clarification")

    words = req.value.strip().lower().split()
    bare_word = words[0] if len(words) == 1 and words[0].isalpha() else None
    if bare_word and is_known_unit(bare_word):
        new_intent, metric = {}, bare_word  # "km", "Mins": no LLM needed
    else:
        try:
            new_intent = drafting.call_llm(
                original_text=audit.user_input,
                clarification=req.value,
                current_draft=drafting.llm_view(intent),
                habits=drafting.habits_context(db),
            )
            metric = new_intent.get("metric") or new_intent.get("suggested_metric")
        except Exception:
            # LLM unavailable: a single word ("reps") is still usable as typed.
            new_intent, metric = {}, bare_word

    if not metric:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=f"Sorry, I didn't catch a unit in “{req.value}”. Try e.g. “km” or “minutes”.",
            draft_sql=audit.draft_sql,
        )

    intent["metric"] = canonical(metric)
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


# ------------------------------------------------------------------
# /today, /stats
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# /reminders
# ------------------------------------------------------------------
_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.IGNORECASE)


def parse_time(value: str) -> dtime:
    """'21:30', '9:30pm', '9pm', '21' -> time. Raises ValueError otherwise."""
    m = _TIME.match(value or "")
    if not m:
        raise ValueError(f"not a time: {value!r}")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"not a time: {value!r}")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"not a time: {value!r}")
    return dtime(hour, minute)


class ReminderRequest(BaseModel):
    chat_id: int
    remind_at: str | None = None  # None with enabled=False turns reminders off
    enabled: bool = True

    @field_validator("remind_at")
    @classmethod
    def _valid_time(cls, v):
        if v is not None:
            parse_time(v)
        return v


def _reminder_json(row: ReminderSetting) -> dict:
    return {
        "chat_id": row.chat_id,
        "remind_at": row.remind_at.strftime("%H:%M"),
        "enabled": row.enabled,
    }


@router.get("/reminders")
def list_reminders(db: Session = Depends(get_db)):
    rows = db.execute(select(ReminderSetting).order_by(ReminderSetting.chat_id)).scalars()
    return [_reminder_json(r) for r in rows]


@router.put("/reminders")
def set_reminder(req: ReminderRequest, db: Session = Depends(get_db)):
    existing = db.get(ReminderSetting, req.chat_id)
    if req.remind_at is None:
        if req.enabled:
            raise HTTPException(status_code=422, detail="remind_at is required")
        if existing is None:
            return {"chat_id": req.chat_id, "remind_at": None, "enabled": False}
        existing.enabled = False
        existing.updated_at = func.now()
        db.commit()
        db.refresh(existing)
        return _reminder_json(existing)

    at = parse_time(req.remind_at)
    db.execute(
        insert(ReminderSetting)
        .values(chat_id=req.chat_id, remind_at=at, enabled=req.enabled)
        .on_conflict_do_update(
            index_elements=[ReminderSetting.chat_id],
            set_={"remind_at": at, "enabled": req.enabled, "updated_at": func.now()},
        )
    )
    db.commit()
    db.expire_all()  # the upsert bypassed the session; don't serve a stale row
    return _reminder_json(db.get(ReminderSetting, req.chat_id))


@router.get("/reminders/check")
def check_reminder(chat_id: int, db: Session = Depends(get_db)):
    return reminder_check(db)
