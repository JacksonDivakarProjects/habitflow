"""Questions about the logs, and the single entry point for a chat message.

/message classifies a message and hands it on: a log becomes a draft (as
/draft does), a question becomes an answer (as /ask does), and small talk
gets the help text. The bot only has to render what comes back.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import drafting, querying
from app.db import get_db
from app.intents import classify
from app.models import QueryLog
from app.routes.drafts import DraftFailed, DraftRequest, DraftResponse, create_draft
from app.routes.edits import EditRequest, EditResponse, start_edit

router = APIRouter(prefix="/internal", tags=["questions"])

HELP_TEXT = (
    "Tell me what you did and I'll log it: “ran 5 km”, “read 20 pages yesterday”, "
    "“meditated 15 min and 30 pushups”.\n"
    "Or ask about your routine: “how much did I read this month?”, "
    "“what's my running pattern?”, “how many hours did I work last week?”.\n"
    "Or fix a saved log: “change yesterday's run to 6 km”, “delete Monday's reading”."
)
LOG_FAILED = (
    "I couldn't turn that into a log. Try “ran 5 km” or “read 20 pages yesterday”, "
    "or ask a question like “how much did I run this week?”."
)


class AskRequest(BaseModel):
    chat_id: int
    text: str


class AskResponse(BaseModel):
    query_id: int | None
    ok: bool
    answer: str
    source: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    truncated: bool = False


def _answer(req: AskRequest, db: Session) -> AskResponse:
    result = querying.ask(req.text, req.chat_id, db, drafting.habits_context(db))
    return AskResponse(
        query_id=result.query_id, ok=result.ok, answer=result.answer, source=result.source,
        sql=result.sql, columns=result.columns, rows=result.rows,
        row_count=result.row_count, truncated=result.truncated,
    )


@router.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, db: Session = Depends(get_db)):
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="Empty question")
    return _answer(req, db)


@router.get("/queries/{query_id}")
def get_query(query_id: int, db: Session = Depends(get_db)):
    entry = db.get(QueryLog, query_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Query not found")
    return {
        "query_id": entry.query_id,
        "question": entry.question,
        "sql": entry.sql,
        "source": entry.source,
        "row_count": entry.row_count,
        "answer": entry.answer,
    }


class MessageRequest(BaseModel):
    chat_id: int
    text: str
    message_id: int | None = None


class MessageResponse(BaseModel):
    kind: str  # log | answer | edit | chat | error
    classified_by: str
    draft: DraftResponse | None = None
    answer: AskResponse | None = None
    edit: EditResponse | None = None
    text: str | None = None


@router.post("/message", response_model=MessageResponse)
def message(req: MessageRequest, db: Session = Depends(get_db)):
    decision = classify(req.text, drafting.habits_context(db))
    if decision.kind == "chat":
        return MessageResponse(kind="chat", classified_by=decision.source, text=HELP_TEXT)
    if decision.kind == "edit":
        return MessageResponse(
            kind="edit", classified_by=decision.source,
            edit=start_edit(EditRequest(chat_id=req.chat_id, text=req.text), db),
        )
    if decision.kind == "query":
        return MessageResponse(
            kind="answer", classified_by=decision.source,
            answer=_answer(AskRequest(chat_id=req.chat_id, text=req.text), db),
        )
    try:
        draft = create_draft(
            DraftRequest(chat_id=req.chat_id, text=req.text, message_id=req.message_id), db
        )
    except DraftFailed:
        return MessageResponse(kind="error", classified_by=decision.source, text=LOG_FAILED)
    return MessageResponse(kind="log", classified_by=decision.source, draft=draft)
