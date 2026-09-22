from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.client import extract_intent

app = FastAPI(title="HabitFlow LLM Service")


class ExtractRequest(BaseModel):
    user_text: str | None = None
    habits: list[dict]
    previous_intent: dict | None = None
    previous_error: str | None = None
    clarification: str | None = None
    original_text: str | None = None


class ExtractResponse(BaseModel):
    intent: dict


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
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    return ExtractResponse(intent=intent)