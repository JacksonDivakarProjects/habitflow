"""
Regex fallback used when the LLM is unreachable. Deliberately simple: it
knows the seeded habits' aliases, numbers, units and relative dates.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from app.timeutil import today
from app.units import ALIASES as UNIT_ALIASES
from app.units import canonical


@dataclass
class ParsedIntent:
    habit_name: str
    amount: Decimal
    metric: Optional[str]     # None if user didn't specify
    log_date: str
    confidence: float


ALIASES = {
    "running":           ["running", "ran", "run", "jog", "jogged", "jogging"],
    "reading":           ["reading", "read"],
    "learning_sql":      ["sql"],
    "learning_concepts": ["concepts", "concept"],
    "reels":             ["reels", "reel", "instagram", "shorts"],
    "meditation":        ["meditation", "meditated", "meditate", "meditating"],
}

_NUMBER = r"\d+(?:\.\d+)?"
# "not running", "not 4", "not 4 miles", "instead of 4 km": what the user is ruling out.
_NEGATED = re.compile(
    rf"\b(?:not|instead of|rather than)\s+(?:{_NUMBER}\s*[a-z]*|[a-z]+)"
)
_DATE_NUMBER = re.compile(r"\b\d+\s+(?:days?|weeks?)\s+ago\b")  # "2 days ago" isn't an amount


def _blank(pattern: re.Pattern, text: str) -> str:
    """Replace matches with spaces, keeping positions stable."""
    return pattern.sub(lambda m: " " * len(m.group()), text)


def _positive_part(lowered: str) -> str:
    """The text without the parts the user negated."""
    return _blank(_NEGATED, lowered)


def find_habit(lowered: str) -> Optional[str]:
    """The habit mentioned earliest in the (non-negated) text."""
    text = _positive_part(lowered)
    best = None
    for name, aliases in ALIASES.items():
        for alias in aliases:
            m = re.search(rf"\b{re.escape(alias)}\b", text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), name)
    return best[1] if best else None


def _find_amount(lowered: str) -> Optional[Decimal]:
    """First number that isn't negated or part of a date: "6 miles, not 4" -> 6."""
    text = _positive_part(_blank(_DATE_NUMBER, lowered))
    m = re.search(_NUMBER, text)
    return Decimal(m.group()) if m else None


def find_unit(lowered: str) -> Optional[str]:
    text = _positive_part(lowered)  # "not 4 miles, 6 km" -> km
    # Prefer the word right after a number ("5km", "5 km", "20 min").
    for m in re.finditer(rf"({_NUMBER})\s*([a-z]+)", text):
        if m.group(2) in UNIT_ALIASES:
            return canonical(m.group(2))
    # Otherwise any unit word, ignoring 1-letter aliases ("h" in a stray word).
    for word in re.findall(r"\b[a-z]+\b", text):
        if len(word) > 1 and word in UNIT_ALIASES:
            return canonical(word)
    return None


def _find_date(lowered: str) -> Optional[str]:
    """ISO date for a relative phrase, or None if the text names no date."""
    ref = today()
    if "day before yesterday" in lowered:
        return (ref - timedelta(days=2)).isoformat()
    m = re.search(r"\b(\d+)\s+days?\s+ago\b", lowered)
    if m:
        return (ref - timedelta(days=int(m.group(1)))).isoformat()
    if "yesterday" in lowered:
        return (ref - timedelta(days=1)).isoformat()
    if "tomorrow" in lowered:
        return (ref + timedelta(days=1)).isoformat()
    if re.search(r"\b(today|tonight|this morning)\b", lowered):
        return ref.isoformat()
    return None


def parse_text(text: str) -> Optional[ParsedIntent]:
    lowered = text.lower()

    habit_name = find_habit(lowered)
    if not habit_name:
        return None
    amount = _find_amount(lowered)
    if amount is None:
        return None
    if "tomorrow" in lowered:
        return None  # don't log the future
    metric = find_unit(lowered)

    return ParsedIntent(
        habit_name=habit_name,
        amount=amount,
        metric=metric,
        log_date=_find_date(lowered) or today().isoformat(),
        confidence=0.9 if metric else 0.6,
    )


def parse_correction(text: str) -> dict:
    """Fields a correction mentions, e.g. "no, 6 km yesterday" ->
    {"amount": 6.0, "metric": "km", "log_date": "..."}. Empty if nothing usable."""
    lowered = text.lower()
    fields: dict = {}
    habit = find_habit(lowered)
    if habit:
        fields["habit_name"] = habit
    amount = _find_amount(lowered)
    if amount is not None:
        fields["amount"] = float(amount)
    unit = find_unit(lowered)
    if unit:
        fields["metric"] = unit
    log_date = _find_date(lowered)
    if log_date:
        fields["log_date"] = log_date
    return fields
