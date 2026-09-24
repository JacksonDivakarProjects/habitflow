"""Changing or deleting a saved log: request -> (choose) -> apply -> undo, or cancel."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import drafting, log_edits
from app.db import get_db
from app.log_edits import EditError
from app.models import LogEdit

router = APIRouter(prefix="/internal/edits", tags=["edits"])


class EditRequest(BaseModel):
    chat_id: int
    text: str


class ChooseRequest(BaseModel):
    log_id: int


class EditResponse(BaseModel):
    edit_id: int | None = None
    status: str  # choosing | pending | applied | reverted | cancelled | unclear
    action: str | None = None  # edit | delete
    message: str | None = None
    before: dict | None = None
    after: dict | None = None
    candidates: list[dict] = []
    already: bool = False


def response(edit: LogEdit, db: Session, already: bool = False) -> EditResponse:
    return EditResponse(
        edit_id=edit.edit_id,
        status=edit.status,
        action=edit.action,
        candidates=log_edits.choices(db, edit),
        already=already,
        **log_edits.describe(edit, db),
    )


def start_edit(req: EditRequest, db: Session) -> EditResponse:
    edit, outcome = log_edits.start(db, req.chat_id, req.text, drafting.habits_context(db))
    if edit is None:
        return EditResponse(status="unclear", message=outcome)
    return response(edit, db)


def _run(fn, db: Session, *args):
    try:
        result = fn(db, *args)
    except EditError as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    edit, already = result if isinstance(result, tuple) else (result, False)
    return response(edit, db, already)


@router.post("", response_model=EditResponse)
def create(req: EditRequest, db: Session = Depends(get_db)):
    return start_edit(req, db)


@router.post("/{edit_id}/choose", response_model=EditResponse)
def choose(edit_id: int, req: ChooseRequest, db: Session = Depends(get_db)):
    return _run(log_edits.choose, db, edit_id, req.log_id)


@router.post("/{edit_id}/apply", response_model=EditResponse)
def apply(edit_id: int, db: Session = Depends(get_db)):
    return _run(log_edits.apply, db, edit_id)


@router.post("/{edit_id}/cancel", response_model=EditResponse)
def cancel(edit_id: int, db: Session = Depends(get_db)):
    return _run(log_edits.cancel, db, edit_id)


@router.post("/{edit_id}/undo", response_model=EditResponse)
def undo(edit_id: int, db: Session = Depends(get_db)):
    return _run(log_edits.revert, db, edit_id)
