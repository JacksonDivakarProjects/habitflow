"""Prompts and endpoints for questions: /classify, /query_sql, /answer."""

import json

from fastapi.testclient import TestClient

from app import questions
from app.main import app
from tests.conftest import HABITS

DATES = {
    "today": "2026-09-24", "today_weekday": "Thursday", "yesterday": "2026-09-23",
    "this_week_start": "2026-09-21", "this_month_start": "2026-09-01",
}
PERIOD = {"start": "2026-09-01", "end": "2026-09-24", "label": "this month"}
SQL = "SELECT sum(amount) AS total FROM habit_logs WHERE habit = 'reading'"


def _system(call) -> str:
    return call["messages"][0]["content"]


# ------------------------------------------------------------------
# classify
# ------------------------------------------------------------------
def test_classify_prompt_has_kinds_habits_and_examples():
    prompt = questions.classify_prompt(HABITS)
    assert "- log:" in prompt and "- query:" in prompt and "- chat:" in prompt
    assert "- edit:" in prompt
    assert "- running (display: Running, unit: miles)" in prompt
    assert '"read today" -> query' in prompt


def test_classify(groq):
    groq.replies.append({"kind": "log"})
    assert questions.classify("pushups twenty", HABITS) == {"kind": "log"}
    call = groq.calls[0]
    assert call["messages"][-1] == {"role": "user", "content": "pushups twenty"}
    assert call["temperature"] == 0.0 and call["response_format"] == {"type": "json_object"}


def test_classify_unknown_kind_is_left_to_the_api(groq):
    groq.replies.append({"kind": "maybe"})
    assert questions.classify("hmm", HABITS) == {"kind": None}


# ------------------------------------------------------------------
# query_sql
# ------------------------------------------------------------------
def test_query_prompt_describes_the_view_rules_dates_and_period():
    prompt = questions.query_prompt(HABITS, DATES, PERIOD)
    assert "habit_logs: One row per logged activity" in prompt
    assert "  - amount_in_habit_unit NUMERIC" in prompt
    assert "- today: 2026-09-24" in prompt and "- this_month_start: 2026-09-01" in prompt
    assert "QUESTION_PERIOD: this month: 2026-09-01 to 2026-09-24" in prompt
    assert "never use CURRENT_DATE" in prompt
    assert "Q: what is my reading pattern" in prompt
    assert "daily_logs" not in prompt  # the model is never told about the raw table


def test_query_prompt_without_a_period():
    assert "QUESTION_PERIOD: none stated" in questions.query_prompt(HABITS, DATES, None)


def test_query_sql(groq):
    groq.replies.append({"sql": f"  {SQL}  ", "note": None})
    assert questions.query_sql("how much did I read", HABITS, DATES) == {"sql": SQL, "note": None}
    assert [m["role"] for m in groq.calls[0]["messages"]] == ["system", "user"]


def test_query_sql_retry_shows_the_rejected_sql_and_reason(groq):
    groq.replies.append({"sql": SQL})
    questions.query_sql("q", HABITS, DATES, previous_sql="SELECT * FROM daily_logs",
                        error="only habit_logs, habits can be queried")
    msgs = groq.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert json.loads(msgs[2]["content"]) == {"sql": "SELECT * FROM daily_logs"}
    assert "only habit_logs, habits can be queried" in msgs[3]["content"]


def test_query_sql_null_with_note(groq):
    groq.replies.append({"sql": None, "note": "I only know your habits."})
    assert questions.query_sql("weather?", HABITS, DATES) == {
        "sql": None, "note": "I only know your habits."}


def test_query_sql_garbage_fields_are_normalized(groq):
    groq.replies.append({"sql": 42, "note": ["x"]})
    assert questions.query_sql("q", HABITS, DATES) == {"sql": None, "note": None}


# ------------------------------------------------------------------
# answer
# ------------------------------------------------------------------
def test_answer_sends_rows_and_rules(groq):
    groq.replies.append({"answer": " You read 60 pages this month. "})
    out = questions.answer("how much did I read", SQL, ["total"], [[60]], 1, False, DATES)
    assert out == {"answer": "You read 60 pages this month."}
    call = groq.calls[0]
    assert "Today is 2026-09-24 (Thursday)" in _system(call)
    assert "using ONLY the numbers in ROWS" in _system(call)
    sent = json.loads(call["messages"][-1]["content"])
    assert sent["ROWS"] == [[60]] and sent["TRUNCATED"] is False


def test_answer_missing_text(groq):
    groq.replies.append({"nope": 1})
    assert questions.answer("q", SQL, [], [], 0, False, DATES) == {"answer": ""}


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
def test_endpoints(groq):
    c = TestClient(app)
    groq.replies.extend([{"kind": "chat"}, {"sql": SQL}, {"answer": "60 pages."}])
    assert c.post("/classify", json={"text": "hi", "habits": HABITS}).json() == {"kind": "chat"}
    assert c.post("/query_sql", json={"question": "q", "habits": HABITS, "dates": DATES,
                                      "question_period": PERIOD}).json()["sql"] == SQL
    r = c.post("/answer", json={"question": "q", "sql": SQL, "columns": ["total"],
                                "rows": [[60]], "row_count": 1, "dates": DATES})
    assert r.json() == {"answer": "60 pages."}


def test_endpoint_errors_are_502(groq):
    groq.replies.extend([RuntimeError("groq down"), "not json"])
    c = TestClient(app)
    r = c.post("/classify", json={"text": "hi"})
    assert r.status_code == 502 and "groq down" in r.json()["detail"]
    assert c.post("/query_sql", json={"question": "q", "dates": DATES}).status_code == 502


def test_non_object_json_is_an_error(groq):
    groq.replies.append("[1, 2]")
    r = TestClient(app).post("/classify", json={"text": "hi"})
    assert r.status_code == 502
