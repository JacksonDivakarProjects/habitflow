"""
Prompts for questions about the logs (semantics.yaml: classifier, query, answer).

classify   message -> {"kind": "log" | "query" | "chat"}
query_sql  question -> {"sql": "SELECT ..." | null, "note": str | null}
answer     question + rows -> {"answer": str}

The API validates everything that comes back; these only have to be good
at the job, not trusted.
"""

import json
from typing import Optional

from app.client import SEMANTICS, client
from app.config import settings


def _chat_json(system: str, messages: list[dict], temperature: float = 0.0) -> dict:
    response = client.chat.completions.create(
        model=settings.groq_model_name,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    reply = json.loads(response.choices[0].message.content)
    if not isinstance(reply, dict):
        raise ValueError("the model did not return a JSON object")
    return reply


def _habit_lines(habits: list[dict]) -> str:
    if not habits:
        return "(none yet)"
    return "\n".join(
        f"- {h['name']} (display: {h.get('display_name') or h['name']}, "
        f"unit: {h.get('default_metric') or 'none'})"
        for h in habits
    )


# ------------------------------------------------------------------
# classify
# ------------------------------------------------------------------
def classify_prompt(habits: list[dict]) -> str:
    spec = SEMANTICS["classifier"]
    kinds = "\n".join(f"- {k}: {v}" for k, v in spec["kinds"].items())
    examples = "\n".join(f'"{e["text"]}" -> {e["kind"]}' for e in spec["examples"])
    return f"""You route messages sent to a personal habit tracker.

KINDS:
{kinds}

KNOWN HABITS:
{_habit_lines(habits)}

EXAMPLES:
{examples}

Return ONLY JSON: {{"kind": "log" | "query" | "chat"}}"""


def classify(text: str, habits: list[dict]) -> dict:
    reply = _chat_json(classify_prompt(habits), [{"role": "user", "content": text}])
    kind = reply.get("kind")
    # Anything else is None: the API then falls back (a number means a log).
    return {"kind": kind if kind in ("log", "query", "chat") else None}


# ------------------------------------------------------------------
# query_sql
# ------------------------------------------------------------------
def query_prompt(habits: list[dict], dates: dict, question_period: Optional[dict]) -> str:
    spec = SEMANTICS["query"]
    relations = []
    for rel in spec["relations"]:
        relations.append(f"\n{rel['name']}: {rel['description']}")
        for c in rel["columns"]:
            note = f"  -- {c['description']}" if c.get("description") else ""
            relations.append(f"  - {c['name']} {c['type']}{note}")
    relations_str = "\n".join(relations)
    rules = "\n".join(f"- {r}" for r in spec["rules"])
    examples = "\n\n".join(
        f"Q: {ex['question']}\nSQL:\n{ex['sql'].strip()}" for ex in spec["examples"]
    )
    date_lines = "\n".join(f"- {k}: {v}" for k, v in dates.items())
    period = (
        f"{question_period['label']}: {question_period['start']} to {question_period['end']}"
        if question_period
        else "none stated (use all time unless the question implies one)"
    )
    return f"""You write PostgreSQL for a personal habit tracker, to answer the user's
question about their own logs.

RELATIONS (the only ones you may read):
{relations_str}

KNOWN HABITS (values of habit_logs.habit):
{_habit_lines(habits)}

DATES (use these literals, never CURRENT_DATE/NOW()):
{date_lines}

QUESTION_PERIOD: {period}

RULES:
{rules}

EXAMPLES (dates there are illustrative; use DATES above):
{examples}

Return ONLY JSON:
{{"sql": "<one SELECT>" or null, "note": "<short reason when sql is null>" or null}}"""


def query_sql(
    question: str,
    habits: list[dict],
    dates: dict,
    question_period: Optional[dict] = None,
    previous_sql: Optional[str] = None,
    error: Optional[str] = None,
) -> dict:
    messages = [{"role": "user", "content": question}]
    if previous_sql and error:
        messages.append({"role": "assistant", "content": json.dumps({"sql": previous_sql})})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That SQL was rejected:\n{error}\n\nFix it and return the corrected JSON."
                ),
            }
        )
    reply = _chat_json(query_prompt(habits, dates, question_period), messages)
    sql = reply.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        sql = None
    note = reply.get("note") if isinstance(reply.get("note"), str) else None
    return {"sql": sql.strip() if sql else None, "note": note}


# ------------------------------------------------------------------
# answer
# ------------------------------------------------------------------
def answer_prompt(dates: dict) -> str:
    rules = "\n".join(f"- {r}" for r in SEMANTICS["answer"]["rules"])
    return f"""You explain query results from a personal habit tracker to its user.
Today is {dates.get("today")} ({dates.get("today_weekday")}).

RULES:
{rules}

Return ONLY JSON: {{"answer": "<your reply>"}}"""


def answer(
    question: str,
    sql: Optional[str],
    columns: list[str],
    rows: list[list],
    row_count: int,
    truncated: bool,
    dates: dict,
) -> dict:
    content = json.dumps(
        {
            "QUESTION": question,
            "SQL": sql,
            "COLUMNS": columns,
            "ROWS": rows,
            "ROW_COUNT": row_count,
            "TRUNCATED": truncated,
        },
        default=str,
    )
    reply = _chat_json(answer_prompt(dates), [{"role": "user", "content": content}], 0.3)
    text = reply.get("answer")
    return {"answer": text.strip() if isinstance(text, str) else ""}
