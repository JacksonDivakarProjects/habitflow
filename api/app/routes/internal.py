from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Habit

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/habits")
def list_habits(db: Session = Depends(get_db)):
    rows = db.execute(
        select(Habit).where(Habit.is_active).order_by(Habit.display_name)
    ).scalars().all()
    return [
        {
            "habit_id": h.habit_id,
            "name": h.name,
            "display_name": h.display_name,
            "metric": h.metric,
        }
        for h in rows
    ]


class DraftRequest(BaseModel):
    chat_id: int
    text: str


class DraftResponse(BaseModel):
    audit_id: int
    preview: str
    sql: str


@router.post("/draft", response_model=DraftResponse)
def draft(req: DraftRequest):
    # Phase 5 will implement Loop 1 here.
    raise HTTPException(status_code=501, detail="Loop 1 not implemented yet")


class FeedbackRequest(BaseModel):
    audit_id: int
    feedback: str


@router.post("/feedback", response_model=DraftResponse)
def feedback(req: FeedbackRequest):
    raise HTTPException(status_code=501, detail="Loop 2 not implemented yet")


class ExecuteRequest(BaseModel):
    audit_id: int


@router.post("/execute")
def execute(req: ExecuteRequest):
    raise HTTPException(status_code=501, detail="Execute not implemented yet")