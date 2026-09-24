"""Intent classification: log vs question vs small talk."""

import pytest

from app.intents import classify, classify_rules

HABITS = [
    {"name": "running", "display_name": "Running"},
    {"name": "reading", "display_name": "Reading"},
    {"name": "learning_sql", "display_name": "Learning SQL"},
    {"name": "work", "display_name": "Work"},
    {"name": "meditation", "display_name": "Meditation"},
]


@pytest.mark.parametrize("text", [
    "ran 5 km",
    "Read 20 pages yesterday",
    "meditated 15 min and 30 pushups",
    "5 km",
    "30",
    "2.5",
    "worked 8 hours",
    "sql 2h",
    "did 20 reps today",            # "did" + amount is a log, not "did I...?"
    "ran 500m this morning",
    "read 30 pages, ran 3 miles",
    "total 5 km run today",         # analytic word, but it's an amount + unit
    "meditated 20",                 # inflected habit name, no unit
    "worked 3",
    "reading 15 today",
])
def test_logs(text):
    decided = classify_rules(text, HABITS)
    assert decided and decided.kind == "log", decided


@pytest.mark.parametrize("text", [
    "how much i read this month",
    "How much did I read this month?",
    "what is my reading pattern",
    "how many hours i worked",
    "how many hours did I work last week",
    "did I run today",
    "have I meditated this week",
    "show my running",
    "list everything from yesterday",
    "compare reading and running",
    "what's my longest streak",
    "reading this month?",
    "my average reading per day",
    "running trend",
    "whats my progress",
    "which day do I read the most",
    "how many km did I run in the last 7 days",  # digits, but clearly a question
    "summarize last week",
])
def test_questions(text):
    decided = classify_rules(text, HABITS)
    assert decided and decided.kind == "query", decided


@pytest.mark.parametrize("text", [
    "hi", "Hello!", "thanks", "thank you 🙏", "help", "ok", "good morning",
    "what can you do?", "   ",
])
def test_small_talk(text):
    decided = classify_rules(text, HABITS)
    assert decided and decided.kind == "chat", decided


@pytest.mark.parametrize("text", ["read today", "sql last week", "running", "meditated"])
def test_unclear_messages_are_left_to_the_llm(text):
    assert classify_rules(text, HABITS) is None


def test_llm_decides_unclear_messages(llm_http):
    llm_http.queue("/classify", {"kind": "query"})
    decided = classify("reading lately", HABITS)
    assert (decided.kind, decided.source) == ("query", "llm")
    assert llm_http.payloads("/classify")[0]["text"] == "reading lately"


def test_clear_messages_never_call_the_llm(llm_http):
    classify("ran 5 km", HABITS)
    classify("how much did I read?", HABITS)
    assert llm_http.calls == []


def test_llm_answer_outside_the_contract_falls_back(llm_http):
    llm_http.queue("/classify", {"kind": "delete everything"})
    decided = classify("running", HABITS)
    assert (decided.kind, decided.source) == ("query", "fallback")


@pytest.mark.parametrize("text,kind", [
    ("meditated", "query"),        # no amount: can't be logged anyway, try answering
    ("pushups 20ish", "log"),      # a number: most likely a log
])
def test_llm_down_fallback(text, kind):
    decided = classify(text, HABITS)
    assert (decided.kind, decided.source) == (kind, "fallback")


def test_llm_without_a_kind_falls_back(llm_http):
    llm_http.queue("/classify", {"kind": None})
    decided = classify("did 20ish", HABITS)
    assert (decided.kind, decided.source) == ("log", "fallback")
