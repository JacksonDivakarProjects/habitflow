"""
What does a message want?

  log    "ran 5 km", "read 20 pages yesterday", "30"
  edit   "change yesterday's run to 6 km", "delete Monday's reading"
  query  "how much did I read this month?", "what's my reading pattern"
  chat   "hi", "thanks", "help"

Clear cases are decided by rules (instant, no LLM). Only messages the rules
can't place ("read today", "sql last week") go to the LLM's /classify; if the
LLM is down, a number means a log and anything else is treated as a question.
"""

import re
from dataclasses import dataclass

from app import llm_client
from app.log_edits import looks_like_edit
from app.querying import match_habits
from app.units import ALIASES as UNIT_ALIASES

KINDS = ("log", "query", "edit", "chat")

_CHAT = re.compile(
    r"^(hi+|hello|hey+|yo|hola|thanks|thank you|thank u|thx|ty|ok|okay|k|cool|nice|great|"
    r"good (morning|afternoon|evening|night)|gm|gn|help|start|menu|"
    r"what can you do|who are you|what are you)[\s!.?🙂👍🙏]*$"
)
_QUESTION_START = re.compile(
    r"^(how|what|what's|whats|when|which|who|why|where|show|list|compare|tell|give me|"
    r"summari[sz]e|summary|analy[sz]e|report|"
    r"(did|do|have|has|am|was|were|is|are)\s+(i|my|me)\b)"
)
_ANALYTIC = re.compile(
    r"\b(how much|how many|how often|how long|pattern|patterns|trend|trends|average|avg|"
    r"mean|summary|stats|statistics|streak|streaks|progress|history|breakdown|most|least|"
    r"best|worst|compare|comparison|so far|consistency|consistent|per day|per week|"
    r"per month|daily|weekly|monthly|in total|overall)\b"
)
_NUMBER = re.compile(r"\d")
_WORD = re.compile(r"[a-z]+")


@dataclass
class Classification:
    kind: str  # log | query | edit | chat
    source: str  # rules | llm | fallback
    reason: str


def _mentions_log_word(lowered: str, habits: list[dict]) -> bool:
    """A habit ("meditated" -> meditation, "worked" -> work) or a unit is named."""
    words = set(_WORD.findall(lowered))
    return (
        bool(match_habits(lowered, habits))
        or any(w in UNIT_ALIASES for w in words)
        or bool(re.search(r"\d+\s*(m|km|h|hr|hrs|min|mins)\b", lowered))
    )


def classify_rules(text: str, habits: list[dict]) -> Classification | None:
    lowered = " ".join(text.lower().split())
    if not lowered:
        return Classification("chat", "rules", "empty message")
    if _CHAT.match(lowered):
        return Classification("chat", "rules", "greeting or help")
    if looks_like_edit(lowered):
        return Classification("edit", "rules", "changes or deletes a saved log")
    if lowered.endswith("?"):
        return Classification("query", "rules", "ends with a question mark")
    if _QUESTION_START.match(lowered):
        return Classification("query", "rules", "starts like a question")
    if _NUMBER.search(lowered):
        if _mentions_log_word(lowered, habits) or re.fullmatch(r"[\d.,\s]+", lowered):
            return Classification("log", "rules", "an amount with a habit or unit")
    if _ANALYTIC.search(lowered):
        return Classification("query", "rules", "asks for totals or patterns")
    return None


def classify(text: str, habits: list[dict]) -> Classification:
    decided = classify_rules(text, habits)
    if decided:
        return decided
    try:
        kind = llm_client.post("/classify", {"text": text, "habits": habits}).get("kind")
    except llm_client.LLMUnavailable:
        kind = None
    else:
        if kind in KINDS:
            return Classification(kind, "llm", "classified by the LLM")
    if _NUMBER.search(text):
        return Classification("log", "fallback", "has a number (LLM unavailable)")
    return Classification("query", "fallback", "no amount (LLM unavailable)")
