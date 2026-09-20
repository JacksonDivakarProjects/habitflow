from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog, DailyLog, Habit
from app.parser import UNIT_MAP, parse_text

router = APIRouter(prefix="/internal", tags=["internal"])


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


class DraftResponse(BaseModel):
    audit_id: int
    preview: str | None = None
    intent: dict | None = None
    needs_input: str | None = None
    prompt: str | None = None


@router.post("/draft", response_model=DraftResponse)
def draft(req: DraftRequest, db: Session = Depends(get_db)):
    parsed = parse_text(req.text)
    if parsed is None:
        audit = AuditLog(
            chat_id=req.chat_id,
            user_input=req.text,
            status="failed",
            error_message="Could not parse a habit and amount from the input",
            iteration_count=1,
        )
        db.add(audit)
        db.commit()
        raise HTTPException(
            status_code=422,
            detail="Couldn't understand. Try: 'ran 4 miles today'",
        )

    habit = db.execute(
        select(Habit).where(Habit.name == parsed.habit_name)
    ).scalar_one_or_none()
    if habit is None or not habit.is_active:
        raise HTTPException(
            status_code=422, detail=f"Unknown habit: {parsed.habit_name}"
        )

    intent = {
        "habit_name": parsed.habit_name,
        "habit_id": habit.habit_id,
        "amount": float(parsed.amount),
        "metric": parsed.metric,
        "log_date": parsed.log_date,
        "confidence": parsed.confidence,
        "raw_text": req.text,
    }

    # No unit given → ask
    if parsed.metric is None:
        audit = AuditLog(
            chat_id=req.chat_id,
            user_input=req.text,
            intent={**intent, "needs": "unit"},
            status="awaiting_input",
            iteration_count=1,
        )
        db.add(audit)
        db.commit()
        db.refresh(audit)

        return DraftResponse(
            audit_id=audit.audit_id,
            needs_input="unit",
            prompt=(
                f"Got it: {parsed.amount:g} of {habit.display_name} "
                f"on {parsed.log_date}. What unit? "
                f"(e.g. {habit.metric})"
            ),
        )

    # Full intent → pending card
    preview = (
        f"{parsed.amount:g} {parsed.metric} of {habit.display_name} "
        f"on {parsed.log_date}"
    )
    audit = AuditLog(
        chat_id=req.chat_id,
        user_input=req.text,
        intent=intent,
        status="pending",
        iteration_count=1,
    )
    db.add(audit)
    db.commit()
    db.refresh(audit)

    return DraftResponse(audit_id=audit.audit_id, preview=preview, intent=intent)


# ------------------------------------------------------------------
# /clarify
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
        raise HTTPException(
            status_code=409, detail=f"Audit status is {audit.status}"
        )

    intent = dict(audit.intent or {})
    needs = intent.get("needs")

    if needs == "unit":
        word = req.value.strip().lower()
        resolved = UNIT_MAP.get(word)
        if resolved is None and word.endswith("s"):
            resolved = UNIT_MAP.get(word[:-1])
        if resolved is None:
            return DraftResponse(
                audit_id=audit.audit_id,
                needs_input="unit",
                prompt=(
                    f"Didn't recognize '{req.value}'. "
                    f"Try one of: pages, hours, minutes, miles, km, concepts."
                ),
            )
        intent["metric"] = resolved
        intent.pop("needs", None)
    else:
        raise HTTPException(
            status_code=422, detail=f"Unknown clarification: {needs}"
        )

    audit.intent = intent
    audit.status = "pending"
    db.commit()
    db.refresh(audit)

    habit = db.get(Habit, intent["habit_id"])
    preview = (
        f"{intent['amount']:g} {intent['metric']} of {habit.display_name} "
        f"on {intent['log_date']}"
    )

    return DraftResponse(audit_id=audit.audit_id, preview=preview, intent=intent)


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
# /feedback (Phase 6)
# ------------------------------------------------------------------
class FeedbackRequest(BaseModel):
    audit_id: int
    feedback: str


@router.post("/feedback")
def feedback(req: FeedbackRequest, db: Session = Depends(get_db)):
    raise HTTPException(
        status_code=501, detail="Feedback loop implemented in Phase 6"
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