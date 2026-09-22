import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

from app.timeutil import today


@dataclass
class ParsedIntent:
    habit_name: str
    amount: Decimal
    metric: Optional[str]     # None if user didn't specify
    log_date: str
    confidence: float


ALIASES = {
    "running":           ["running", "ran", "run", "jog", "jogged", "walked", "walk"],
    "reading":           ["reading", "read", "book", "books"],
    "learning_sql":      ["sql"],
    "learning_concepts": ["concepts", "concept"],
    "reels":             ["reels", "reel", "instagram", "shorts"],
    "meditation":        ["meditation", "meditated", "meditate"],
}

UNIT_MAP = {
    "mile":     "miles",
    "miles":    "miles",
    "mi":       "miles",
    "page":     "pages",
    "pages":    "pages",
    "book":     "books",
    "books":    "books",
    "hour":     "hours",
    "hours":    "hours",
    "hr":       "hours",
    "hrs":      "hours",
    "minute":   "minutes",
    "minutes":  "minutes",
    "min":      "minutes",
    "mins":     "minutes",
    "second":   "seconds",
    "seconds":  "seconds",
    "sec":      "seconds",
    "secs":     "seconds",
    "concept":  "concepts",
    "concepts": "concepts",
    "km":       "km",
    "kilometer": "km",
    "kilometers": "km",
}


def parse_text(text: str) -> Optional[ParsedIntent]:
    lowered = text.lower()

    # 1. Find habit
    habit_name = None
    for name, aliases in ALIASES.items():
        if any(re.search(rf"\b{re.escape(a)}\b", lowered) for a in aliases):
            habit_name = name
            break
    if not habit_name:
        return None

    # 2. Find amount
    amount_match = re.search(r"(\d+(?:\.\d+)?)", lowered)
    if not amount_match:
        return None
    amount = Decimal(amount_match.group(1))

    # 3. Find unit — leave as None if not stated
    metric = None
    for word in re.findall(r"\b[a-z]+\b", lowered):
        if word in UNIT_MAP:
            metric = UNIT_MAP[word]
            break

    # 4. Find date
    log_date = today()
    if "yesterday" in lowered:
        log_date = log_date - timedelta(days=1)
    elif "tomorrow" in lowered:
        return None   # don't log the future

    return ParsedIntent(
        habit_name=habit_name,
        amount=amount,
        metric=metric,
        log_date=log_date.isoformat(),
        confidence=0.9 if metric else 0.6,
    )
