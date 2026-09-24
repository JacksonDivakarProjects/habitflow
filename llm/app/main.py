from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import questions
from app.client import extract_intent

app = FastAPI(title="HabitFlow LLM Service")


class ExtractRequest(BaseModel):
    user_text: str | None = None
    habits: list[dict]
    previous_intent: dict | None = None
    previous_error: str | None = None
    clarification: str | None = None
    original_text: str | None = None
    current_draft: dict | None = None


class ExtractResponse(BaseModel):
    intent: dict


class ClassifyRequest(BaseModel):
    text: str
    habits: list[dict] = []


class QuerySqlRequest(BaseModel):
    question: str
    habits: list[dict] = []
    dates: dict
    question_period: dict | None = None
    previous_sql: str | None = None
    error: str | None = None


class AnswerRequest(BaseModel):
    question: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0
    truncated: bool = False
    dates: dict = {}


def _call(fn, **kwargs) -> dict:
    try:
        return fn(**kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    try:
        intent = extract_intent(
            user_text=req.user_text or "",
            habits=req.habits,
            previous_intent=req.previous_intent,
            previous_error=req.previous_error,
            clarification=req.clarification,
            original_text=req.original_text,
            current_draft=req.current_draft,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}") from e
    return ExtractResponse(intent=intent)


@app.post("/classify")
def classify(req: ClassifyRequest):
    return _call(questions.classify, text=req.text, habits=req.habits)


@app.post("/query_sql")
def query_sql(req: QuerySqlRequest):
    return _call(questions.query_sql, **req.model_dump())


@app.post("/answer")
def answer(req: AnswerRequest):
    return _call(questions.answer, **req.model_dump())
