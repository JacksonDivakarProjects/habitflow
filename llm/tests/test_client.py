from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import client
from app.main import app
from tests.conftest import HABITS

INTENT = {"habit_name": "running", "amount": 4, "metric": "miles"}


def _roles(call):
    return [m["role"] for m in call["messages"]]


def test_system_prompt_lists_habits_with_units():
    prompt = client._build_system_prompt(HABITS)

    assert "- running (display: Running, default_metric: miles, recently_used: km)" in prompt
    assert "- reading (display: Reading, default_metric: none, recently_used: none)" in prompt


def test_system_prompt_without_habits():
    assert "(none yet" in client._build_system_prompt([])


def test_system_prompt_includes_schema_rules_and_examples():
    prompt = client._build_system_prompt(HABITS)

    assert "TABLE daily_logs" in prompt
    assert "habit_id INTEGER REFERENCES habits.habit_id" in prompt
    assert "never use DROP, DELETE, UPDATE, or TRUNCATE" in prompt
    assert "Intent: log 4 miles of running today" in prompt
    assert '"draft_sql"' in prompt


def test_system_prompt_dates_use_app_timezone():
    local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

    prompt = client._build_system_prompt(HABITS)

    assert f"Today: {local_today.isoformat()}" in prompt
    assert f"Yesterday: {(local_today - timedelta(days=1)).isoformat()}" in prompt


def test_plain_extraction(groq):
    groq.replies.append(INTENT)

    assert client.extract_intent("ran 4 miles", HABITS) == INTENT

    call = groq.calls[0]
    assert _roles(call) == ["system", "user"]
    assert call["messages"][1]["content"] == "ran 4 miles"
    assert call["response_format"] == {"type": "json_object"}
    assert call["temperature"] == 0.1


def test_retry_shows_previous_answer_and_error(groq):
    groq.replies.append(INTENT)

    client.extract_intent(
        "ran 4 miles", HABITS,
        previous_intent={"amount": "four"},
        previous_error="Amount must be a number, got 'four'.",
    )

    msgs = groq.calls[0]["messages"]
    assert _roles(groq.calls[0]) == ["system", "user", "assistant", "user"]
    assert msgs[2]["content"] == '{"amount": "four"}'
    assert "rejected by validation" in msgs[3]["content"]
    assert "got 'four'" in msgs[3]["content"]
    assert "dry-run" not in msgs[3]["content"]


def test_clarification_shows_current_draft(groq):
    groq.replies.append(INTENT)

    client.extract_intent(
        "", HABITS,
        original_text="ran 4",
        clarification="km",
        current_draft={"habit_name": "running", "amount": 4, "metric": None},
    )

    msgs = groq.calls[0]["messages"]
    assert _roles(groq.calls[0]) == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "ran 4"
    assert '"amount": 4' in msgs[2]["content"]
    assert msgs[3]["content"] == "km"


def test_clarification_without_draft_uses_placeholder(groq):
    groq.replies.append(INTENT)

    client.extract_intent("", HABITS, original_text="ran 4", clarification="km")

    assert "clarification needed" in groq.calls[0]["messages"][2]["content"]


@pytest.fixture
def api():
    return TestClient(app)


def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_extract_endpoint(api, groq):
    groq.replies.append(INTENT)

    r = api.post("/extract", json={"user_text": "ran 4 miles", "habits": HABITS})

    assert r.status_code == 200
    assert r.json() == {"intent": INTENT}


def test_extract_endpoint_passes_current_draft(api, groq):
    groq.replies.append(INTENT)

    api.post("/extract", json={
        "habits": HABITS,
        "original_text": "ran 4",
        "clarification": "no, 6",
        "current_draft": {"amount": 4},
    })

    assert groq.calls[0]["messages"][2]["content"] == '{"amount": 4}'


def test_extract_endpoint_groq_error_is_502(api, groq):
    groq.replies.append(RuntimeError("rate limited"))

    r = api.post("/extract", json={"user_text": "x", "habits": []})

    assert r.status_code == 502
    assert "rate limited" in r.json()["detail"]


def test_extract_endpoint_non_json_reply_is_502(api, groq):
    groq.replies.append("Sure! Here's your JSON: {")

    r = api.post("/extract", json={"user_text": "x", "habits": []})

    assert r.status_code == 502


def test_system_prompt_explains_multi_habit_messages():
    prompt = client._build_system_prompt(HABITS)

    assert '"extra_logs"' in prompt
    assert "several habits in one message" in prompt
    assert ":amount_2" in prompt  # the multi-row SQL example


def test_system_prompt_gives_llm_the_unit_decisions():
    prompt = client._build_system_prompt(HABITS)

    assert "UNITS:" in prompt
    assert "Preferred unit names: miles, km, meters, minutes" in prompt
    assert '"ran 500 m" (habit default: miles) -> metric: meters' in prompt
    assert '"did 20 pushups" (habit default: (new habit)) -> metric: null, suggested_metric: reps' in prompt
    assert "never convert" in prompt
