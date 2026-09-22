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
_NEGATED = re.compile(rf"\b(?:not|instead of|rather than)\s+({_NUMBER})")
_DATE_NUMBER = re.compile(r"\b\d+\s+(?:days?|weeks?)\s+ago\b")  # "2 days ago" isn't an amount


def _find_habit(lowered: str) -> Optional[str]:
    for name, aliases in ALIASES.items():
        if any(re.search(rf"\b{re.escape(a)}\b", lowered) for a in aliases):
            return name
    return None


def _find_amount(lowered: str) -> Optional[Decimal]:
    """First number that isn't negated or part of a date: "6 miles, not 4" -> 6."""
    lowered = _DATE_NUMBER.sub(lambda m: " " * len(m.group()), lowered)
    negated = {m.start(1) for m in _NEGATED.finditer(lowered)}
    for m in re.finditer(_NUMBER, lowered):
        if m.start() not in negated:
            return Decimal(m.group())
    return None


def _find_unit(lowered: str) -> Optional[str]:
    # Prefer the word right after a number ("5km", "5 km", "20 min").
    for m in re.finditer(rf"({_NUMBER})\s*([a-z]+)", lowered):
        if m.group(2) in UNIT_ALIASES:
            return canonical(m.group(2))
    # Otherwise any unit word, ignoring 1-letter aliases ("m" in "I'm").
    for word in re.findall(r"\b[a-z]+\b", lowered):
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

    habit_name = _find_habit(lowered)
    if not habit_name:
        return None
    amount = _find_amount(lowered)
    if amount is None:
        return None
    if "tomorrow" in lowered:
        return None  # don't log the future
    metric = _find_unit(lowered)

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
    habit = _find_habit(lowered)
    if habit:
        fields["habit_name"] = habit
    amount = _find_amount(lowered)
    if amount is not None:
        fields["amount"] = float(amount)
    unit = _find_unit(lowered)
    if unit:
        fields["metric"] = unit
    log_date = _find_date(lowered)
    if log_date:
        fields["log_date"] = log_date
    return fields
