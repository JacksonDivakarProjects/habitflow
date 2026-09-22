import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import drafting
from app.db import get_db
from app.drafting import regenerate_from_feedback, run_loop1
from app.models import AuditLog, DailyLog, Habit

router = APIRouter(prefix="/internal", tags=["internal"])


def _record_duration(audit: AuditLog, started: float, db: Session):
    """Store how long drafting took (all LLM attempts) on the audit row."""
    audit.duration_ms = int((time.monotonic() - started) * 1000)
    db.commit()
    db.refresh(audit)


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


@router.post("/draft", response_model=DraftResponse)
def draft(req: DraftRequest, db: Session = Depends(get_db)):
    started = time.monotonic()
    audit, clarify_prompt, proposal_prompt = run_loop1(req.text, req.chat_id, db)
    audit.message_id = req.message_id
    _record_duration(audit, started, db)

    if clarify_prompt:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=clarify_prompt,
            draft_sql=audit.draft_sql,
        )
    if proposal_prompt:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="habit",
            prompt=proposal_prompt,
            draft_sql=audit.draft_sql,
        )
    if audit.status == "failed":
        raise HTTPException(
            status_code=422,
            detail=audit.error_message or "Couldn't understand that.",
        )

    habit = db.get(Habit, audit.intent["habit_id"])
    source = audit.intent.get("metric_source", "explicit")
    preview = (
        f"{audit.intent['amount']:g} {audit.intent['metric']} of "
        f"{habit.display_name} on {audit.intent['log_date']}"
    )
    return DraftResponse(
        audit_id=audit.audit_id,
        preview=preview,
        intent=audit.intent,
        draft_sql=audit.draft_sql,
        metric_source=source,
    )


# ------------------------------------------------------------------
# /clarify — missing unit
# ------------------------------------------------------------------
class ClarifyRequest(BaseModel):
    audit_id: int
    value: str


@router.post("/clarify", response_model=DraftResponse)
def clarify(req: ClarifyRequest, db: Session = Depends(get_db)):
    audit = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.audit_id == req.audit_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    if audit.status != "awaiting_input":
        raise HTTPException(status_code=409, detail=f"Status is {audit.status}")

    intent = dict(audit.intent or {})
    if intent.get("needs") != "unit":
        raise HTTPException(status_code=409, detail="Not a unit clarification")

    try:
        new_intent = drafting.call_llm(
            original_text=audit.user_input,
            clarification=req.value,
            current_draft=drafting.llm_view(intent),
            habits=drafting.habits_context(db),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e

    metric = new_intent.get("metric") or new_intent.get("suggested_metric")
    if not metric:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=f"Still need a unit for '{req.value}'. Try again.",
            draft_sql=audit.draft_sql,
        )

    intent["metric"] = str(metric).strip().lower()
    intent["metric_source"] = "explicit"
    if new_intent.get("draft_sql"):
        intent["draft_sql"] = new_intent["draft_sql"]
    intent.pop("needs", None)

    drafting.supersede_pending(audit.chat_id, db, keep_audit_id=audit.audit_id)
    audit.intent = intent
    audit.draft_sql = intent.get("draft_sql") or audit.draft_sql
    audit.status = "pending"
    db.commit()
    db.refresh(audit)

    habit = db.get(Habit, intent["habit_id"])
    preview = (
        f"{intent['amount']:g} {intent['metric']} of "
        f"{habit.display_name} on {intent['log_date']}"
    )
    return DraftResponse(
        audit_id=audit.audit_id,
        preview=preview,
        intent=intent,
        draft_sql=audit.draft_sql,
        metric_source="explicit",
    )


# ------------------------------------------------------------------
# /approve_habit — create a proposed habit
# ------------------------------------------------------------------
class ApproveHabitRequest(BaseModel):
    audit_id: int
    accept: bool


@router.post("/approve_habit", response_model=DraftResponse)
def approve_habit(req: ApproveHabitRequest, db: Session = Depends(get_db)):
    audit = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.audit_id == req.audit_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    if audit.status != "awaiting_input":
        raise HTTPException(status_code=409, detail=f"Status is {audit.status}")

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
    default_metric = intent.get("metric") or intent.get("suggested_metric")

    existing = db.execute(select(Habit).where(Habit.name == name)).scalar_one_or_none()
    if existing:
        habit = existing
    else:
        habit = Habit(
            name=name,
            display_name=name.replace("_", " ").title(),
            metric=default_metric,
        )
        db.add(habit)
        db.flush()

    intent["habit_name"] = name
    intent["habit_id"] = habit.habit_id
    intent.pop("proposed_habit", None)
    intent.pop("needs", None)

    metric = default_metric
    source = "suggested" if default_metric else "missing"

    if metric is None:
        intent["needs"] = "unit"
        audit.intent = intent
        audit.status = "awaiting_input"
        db.commit()
        db.refresh(audit)
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=(
                f"Created '{habit.display_name}'. What unit for "
                f"{intent['amount']:g} on {intent['log_date']}?"
            ),
            draft_sql=audit.draft_sql,
        )

    intent["metric"] = metric
    intent["metric_source"] = source
    drafting.supersede_pending(audit.chat_id, db, keep_audit_id=audit.audit_id)
    audit.intent = intent
    audit.status = "pending"
    db.commit()
    db.refresh(audit)

    preview = (
        f"{intent['amount']:g} {intent['metric']} of "
        f"{habit.display_name} on {intent['log_date']}"
    )
    return DraftResponse(
        audit_id=audit.audit_id,
        preview=preview,
        intent=intent,
        draft_sql=audit.draft_sql,
        metric_source=source,
    )


# ------------------------------------------------------------------
# /execute
# ------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    audit_id: int


@router.post("/execute")
def execute(req: ExecuteRequest, db: Session = Depends(get_db)):
    audit = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.audit_id == req.audit_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")

    if audit.status == "executed":
        return {"status": "already_executed", "log_id": None}
    if audit.status != "pending":
        raise HTTPException(status_code=409, detail=f"Audit status is {audit.status}")

    intent = audit.intent or {}
    habit_id = intent.get("habit_id")
    amount = intent.get("amount")
    metric = intent.get("metric")
    log_date = intent.get("log_date")

    if not all([habit_id, amount, metric, log_date]):
        audit.status = "failed"
        audit.error_message = "Incomplete intent"
        db.commit()
        raise HTTPException(status_code=422, detail="Intent incomplete")

    log = DailyLog(
        habit_id=habit_id,
        amount=amount,
        metric=metric,
        log_date=log_date,
        source="llm",
        raw_input=audit.user_input,
        audit_id=audit.audit_id,
    )
    db.add(log)
    db.flush()

    audit.status = "executed"
    audit.final_sql = (
        "INSERT INTO daily_logs "
        "(habit_id, amount, metric, log_date, source, audit_id) "
        "VALUES (:habit_id, :amount, :metric, :log_date, 'llm', :audit_id)"
    )
    db.commit()

    return {"status": "executed", "log_id": log.log_id}


# ------------------------------------------------------------------
# /feedback — Loop 2 (user correction)
# ------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    audit_id: int
    feedback: str


@router.post("/feedback", response_model=DraftResponse)
def feedback(req: FeedbackRequest, db: Session = Depends(get_db)):
    audit = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.audit_id == req.audit_id)
            .with_for_update()
        )
        .scalar_one_or_none()
    )
    if audit is None:
        raise HTTPException(status_code=404, detail="Audit not found")
    if audit.status not in ("pending", "awaiting_input"):
        raise HTTPException(
            status_code=409, detail=f"Audit status is {audit.status}"
        )

    started = time.monotonic()
    audit, clarify_prompt, proposal_prompt = regenerate_from_feedback(
        audit, req.feedback, db
    )
    _record_duration(audit, started, db)

    if clarify_prompt:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=clarify_prompt,
            draft_sql=audit.draft_sql,
        )
    if proposal_prompt:
        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="habit",
            prompt=proposal_prompt,
            draft_sql=audit.draft_sql,
        )
    if audit.status == "failed":
        raise HTTPException(
            status_code=422,
            detail=audit.error_message or "Couldn't regenerate from feedback.",
        )

    habit = db.get(Habit, audit.intent["habit_id"])
    source = audit.intent.get("metric_source", "explicit")
    preview = (
        f"{audit.intent['amount']:g} {audit.intent['metric']} of "
        f"{habit.display_name} on {audit.intent['log_date']}"
    )
    return DraftResponse(
        audit_id=audit.audit_id,
        preview=preview,
        intent=audit.intent,
        draft_sql=audit.draft_sql,
        metric_source=source,
    )


# ------------------------------------------------------------------
# /stats
# ------------------------------------------------------------------
@router.get("/stats")
def stats(chat_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            """
            SELECT h.display_name,
                   dl.metric,
                   SUM(dl.amount) AS total,
                   COUNT(DISTINCT dl.log_date) AS days
            FROM daily_logs dl
            JOIN habits h USING (habit_id)
            WHERE dl.log_date >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY h.display_name, dl.metric
            ORDER BY h.display_name, dl.metric
            """
        )
    ).all()
    return [
        {
            "habit": r.display_name,
            "metric": r.metric,
            "total": float(r.total),
            "days": r.days,
        }
        for r in rows
    ]
