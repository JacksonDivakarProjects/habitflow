"""
Fixing logs that are already saved:

  "change yesterday's run to 6 km"      edit the amount (and unit)
  "yesterday's reading was 30 pages"    same, said differently
  "move today's meditation to yesterday"  edit the date
  "delete Monday's reading"             delete (void) it
  "remove my last run"                  the most recent one

The request is read by rules, without the LLM: which log (habit, date, amount,
or "last"), and what should change. Nothing happens until the user confirms a
card. If several logs match, the user picks one. Applying keeps the old
values, so every change and delete can be undone; a delete voids the log like
/undo does, so it stays in the audit trail.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import DailyLog, Habit, LogEdit
from app.parser import _positive_part
from app.querying import match_habits
from app.timeutil import friendly_date, today
from app.units import ALIASES as UNIT_ALIASES
from app.units import AMBIGUOUS, canonical, quantity, resolve

MAX_CANDIDATES = 6

_DELETE = re.compile(r"\b(delete|remove|erase|undo|scrap|get rid of)\b")
_EDIT = re.compile(r"\b(change|edit|update|fix|correct|make|set|move|adjust|modify)\b")
# "yesterday's run was 6 km", "monday's reading should be 30 pages"
_WAS = re.compile(r"\b(was|were|should be|should have been|is actually|was actually)\b")
# Where the target ends and the new value starts: "<target> to <new>".
_SPLIT = re.compile(
    r"\s(?:to|into|as|should be|should have been|was actually|is actually|was|were)\s|:|→|->"
)
_LATEST = re.compile(
    r"\b(latest|most recent|last|previous)\b(?!\s+(?:\d|week|month|year|"
    + "|".join(d.lower() for d in calendar.day_name)
    + r"))"
)

_WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
_WEEKDAYS.update(
    {
        "mon": 0,
        "tue": 1,
        "tues": 1,
        "wed": 2,
        "thu": 3,
        "thur": 3,
        "thurs": 3,
        "fri": 4,
        "sat": 5,
        "sun": 6,
    }
)
_MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
_MONTHS["sept"] = 9
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

# Date phrases, most specific first. Each returns a date from a match.
_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b"), "day_month"),
    (re.compile(rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b"), "month_day"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})\b"), "slash"),  # day/month
    (re.compile(r"\bday before yesterday\b"), "before_yesterday"),
    (re.compile(r"\b(\d{1,3})\s+days?\s+ago\b"), "days_ago"),
    (re.compile(r"\byesterday(?:'s)?\b"), "yesterday"),
    (re.compile(r"\b(?:today|tonight|this morning|this evening)(?:'s)?\b"), "today"),
    (re.compile(rf"\b(last\s+)?({_WEEKDAY_RE})(?:day)?(?:'s)?\b"), "weekday"),
]
_AMOUNT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([a-z]+)?")


# ------------------------------------------------------------------
# Reading the request
# ------------------------------------------------------------------
def _year_for(month: int, day: int, ref: date) -> Optional[date]:
    try:
        d = date(ref.year, month, day)
        return d if d <= ref else date(ref.year - 1, month, day)
    except ValueError:
        return None


def _date_from(kind: str, m: re.Match, ref: date) -> Optional[date]:
    try:
        if kind == "iso":
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if kind == "day_month":
            return _year_for(_MONTHS[m.group(2)], int(m.group(1)), ref)
        if kind == "month_day":
            return _year_for(_MONTHS[m.group(1)], int(m.group(2)), ref)
        if kind == "slash":
            return _year_for(int(m.group(2)), int(m.group(1)), ref)
    except (ValueError, KeyError):
        return None
    if kind == "before_yesterday":
        return ref - timedelta(days=2)
    if kind == "days_ago":
        return ref - timedelta(days=int(m.group(1)))
    if kind == "yesterday":
        return ref - timedelta(days=1)
    if kind == "today":
        return ref
    if kind == "weekday":
        back = (ref.weekday() - _WEEKDAYS[m.group(2)]) % 7
        if m.group(1) and back == 0:  # "last monday" on a Monday: a week ago
            back = 7
        return ref - timedelta(days=back)
    return None


def find_date(text: str, ref: date) -> tuple[Optional[date], str]:
    """The first date named in text, and the text with date phrases blanked out
    (so "21 sep" or "2 days ago" is never read as an amount)."""
    found = None
    blanked = text
    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(blanked):
            d = _date_from(kind, m, ref)
            if d is not None and (found is None or m.start() < found[0]):
                found = (m.start(), d)
        blanked = pattern.sub(lambda m: " " * len(m.group()), blanked)
    return (found[1] if found else None), blanked


def _amount_and_unit(text: str) -> tuple[Optional[float], Optional[str]]:
    """The number (and unit) that isn't negated: "6 km, not 5" -> (6, "km")."""
    text = _positive_part(text)
    amount, unit = None, None
    for m in _AMOUNT.finditer(text):
        amount = float(m.group(1))
        word = m.group(2)
        unit = canonical(word) if word and (word in UNIT_ALIASES or word in AMBIGUOUS) else None
        break
    if amount is None:  # a unit alone: "change yesterday's run to km"
        for word in re.findall(r"[a-z]+", text):
            if len(word) > 1 and word in UNIT_ALIASES:
                unit = canonical(word)
                break
    return amount, unit


@dataclass
class Request:
    action: str  # edit | delete
    habits: list[str] = field(default_factory=list)
    log_date: Optional[date] = None  # which day's log
    amount: Optional[float] = None  # "the 5 km run"
    latest: bool = False  # "my last run"
    changes: dict = field(default_factory=dict)  # amount / unit / log_date to set

    @property
    def names_a_log(self) -> bool:
        return bool(self.habits or self.log_date or self.amount or self.latest)


def looks_like_edit(lowered: str) -> bool:
    """For the message router: a change to a saved log, not a new log."""
    lowered = re.sub(r"^(please|pls|can you|could you|can u)\s+", "", lowered.strip())
    if re.match(rf"^({_DELETE.pattern}|{_EDIT.pattern})", lowered):
        return True
    # "yesterday's run was 6 km": a past day, then "was" and a number.
    return bool(
        re.search(
            r"(yesterday|today|\bdays? ago|" + _WEEKDAY_RE + r")\S*\s.*\b(was|were|"
            r"should be|should have been)\s+\d",
            lowered,
        )
    )


def parse_request(text: str, habits: list[dict], ref: date) -> Request:
    lowered = " ".join(text.lower().split())
    delete_word, edit_word = _DELETE.search(lowered), _EDIT.search(lowered)
    # Whichever verb comes first wins; "... was 6 km" is always an edit.
    is_delete = (
        delete_word
        and not _WAS.search(lowered)
        and (not edit_word or delete_word.start() < edit_word.start())
    )
    action = "delete" if is_delete else "edit"

    split = _SPLIT.search(lowered) if action == "edit" else None
    target, new = (lowered[: split.start()], lowered[split.end() :]) if split else (lowered, "")

    target_date, target_rest = find_date(target, ref)
    req = Request(
        action=action,
        log_date=target_date,
        habits=match_habits(target, habits) or match_habits(lowered, habits),
        latest=bool(_LATEST.search(target)),
    )
    if split or action == "delete":
        req.amount, _ = _amount_and_unit(target_rest)

    if action == "edit":
        source = new if split else target_rest
        new_date, new_rest = find_date(source, ref) if split else (None, source)
        # Without "to"/"was" ("fix yesterday's run 6 km") the number is the new value.
        amount, unit = _amount_and_unit(new_rest)
        if amount is not None:
            req.changes["amount"] = amount
        if unit:
            req.changes["unit"] = unit
        if new_date:
            req.changes["log_date"] = new_date.isoformat()
    return req


# ------------------------------------------------------------------
# Finding the log
# ------------------------------------------------------------------
def _item(log: DailyLog, habit: Habit) -> dict:
    return {
        "log_id": log.log_id,
        "habit": habit.display_name,
        "amount": float(log.amount),
        "unit": log.metric,
        "quantity": quantity(log.amount, log.metric),
        "log_date": log.log_date.isoformat(),
        "when": friendly_date(log.log_date),
    }


def candidates(db: Session, req: Request) -> list[tuple[DailyLog, Habit]]:
    query = (
        select(DailyLog, Habit)
        .join(Habit, Habit.habit_id == DailyLog.habit_id)
        .where(DailyLog.voided_at.is_(None))
        .order_by(DailyLog.log_date.desc(), DailyLog.log_id.desc())
    )
    if req.habits:
        query = query.where(Habit.name.in_(req.habits))
    if req.log_date:
        query = query.where(DailyLog.log_date == req.log_date)
    if req.amount is not None:
        query = query.where(DailyLog.amount == req.amount)
    rows = db.execute(query.limit(MAX_CANDIDATES)).all()
    return rows[:1] if req.latest and rows else rows


# ------------------------------------------------------------------
# State changes
# ------------------------------------------------------------------
class EditError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _snapshot(log: DailyLog) -> dict:
    return {"amount": float(log.amount), "unit": log.metric, "log_date": log.log_date.isoformat()}


def _new_values(log: DailyLog, habit: Habit, changes: dict) -> dict:
    new = _snapshot(log)
    if "amount" in changes:
        new["amount"] = float(changes["amount"])
    if changes.get("unit"):
        new["unit"] = resolve(changes["unit"], habit.metric)
    if changes.get("log_date"):
        new["log_date"] = changes["log_date"]
    return new


def describe(edit: LogEdit, db: Session) -> dict:
    """The before/after for a card."""
    if not edit.log_id:
        return {"before": None, "after": None}
    log = db.get(DailyLog, edit.log_id)
    habit = db.get(Habit, log.habit_id)
    old = edit.old_values or _snapshot(log)
    before = {**_item(log, habit), **_values_item(old)}
    after = None
    if edit.action == "edit":
        new = edit.new_values or _new_values(log, habit, edit.changes)
        after = {**before, **_values_item(new)}
    return {"before": before, "after": after}


def _values_item(values: dict) -> dict:
    d = date.fromisoformat(values["log_date"])
    return {
        "amount": values["amount"],
        "unit": values["unit"],
        "quantity": quantity(values["amount"], values["unit"]),
        "log_date": values["log_date"],
        "when": friendly_date(d),
    }


def _validate_change(log: DailyLog, habit: Habit, changes: dict) -> Optional[str]:
    if "amount" in changes and not changes["amount"] > 0:
        return "The amount has to be more than 0. To remove the log, say “delete …”."
    new = _new_values(log, habit, changes)
    if date.fromisoformat(new["log_date"]) > today():
        return "That date is in the future."
    if new == _snapshot(log):
        return (
            f"It already says {quantity(new['amount'], new['unit'])} {friendly_date(log.log_date)}."
        )
    return None


def start(
    db: Session, chat_id: int, text: str, habits: list[dict]
) -> tuple[Optional[LogEdit], str]:
    """Create an edit from a request. Returns (edit or None, status/message key)."""
    req = parse_request(text, habits, today())
    if not req.names_a_log:
        return None, (
            "Which log? Say the habit or the day, like “change yesterday's run to 6 km” "
            "or “delete Monday's reading”."
        )
    if req.action == "edit" and not req.changes:
        return None, (
            "What should it be? For example “change yesterday's run to 6 km” "
            "or “move today's reading to yesterday”."
        )
    rows = candidates(db, req)
    if not rows:
        what = " ".join(
            filter(
                None,
                [
                    ", ".join(h.replace("_", " ") for h in req.habits) or "log",
                    friendly_date(req.log_date) if req.log_date else "",
                ],
            )
        )
        return None, f"I couldn't find a {what} to change. /today shows today's logs."

    if len(rows) == 1 and req.action == "edit":
        problem = _validate_change(rows[0][0], rows[0][1], req.changes)
        if problem:
            return None, problem

    # A newer request replaces an older open one.
    db.execute(
        update(LogEdit)
        .where(LogEdit.chat_id == chat_id, LogEdit.status.in_(("choosing", "pending")))
        .values(status="superseded", updated_at=func.now())
    )
    edit = LogEdit(
        chat_id=chat_id,
        request=text,
        action=req.action,
        changes=req.changes,
        candidates=[log.log_id for log, _ in rows],
    )
    if len(rows) == 1:
        edit.log_id, edit.status = rows[0][0].log_id, "pending"
    else:
        edit.status = "choosing"
    db.add(edit)
    db.commit()
    db.refresh(edit)
    return edit, edit.status


def _lock(db: Session, edit_id: int) -> LogEdit:
    edit = db.execute(
        select(LogEdit).where(LogEdit.edit_id == edit_id).with_for_update()
    ).scalar_one_or_none()
    if edit is None:
        raise EditError(404, "That change isn't there anymore.")
    return edit


def _lock_log(db: Session, log_id: int) -> DailyLog:
    return db.execute(
        select(DailyLog).where(DailyLog.log_id == log_id).with_for_update()
    ).scalar_one()


def choose(db: Session, edit_id: int, log_id: int) -> LogEdit:
    edit = _lock(db, edit_id)
    if edit.status != "choosing":
        raise EditError(409, _status_message(edit))
    if log_id not in (edit.candidates or []):
        raise EditError(422, "That log wasn't one of the choices.")
    log = _lock_log(db, log_id)
    if log.voided_at is not None:
        raise EditError(409, "That log was removed in the meantime.")
    if edit.action == "edit":
        problem = _validate_change(log, db.get(Habit, log.habit_id), edit.changes)
        if problem:
            raise EditError(422, problem)
    edit.log_id, edit.status, edit.updated_at = log_id, "pending", func.now()
    db.commit()
    db.refresh(edit)
    return edit


def apply(db: Session, edit_id: int) -> tuple[LogEdit, bool]:
    """Apply a confirmed edit. Returns (edit, already_applied)."""
    edit = _lock(db, edit_id)
    if edit.status == "applied":
        return edit, True
    if edit.status != "pending":
        raise EditError(409, _status_message(edit))
    log = _lock_log(db, edit.log_id)
    habit = db.get(Habit, log.habit_id)
    if log.voided_at is not None:
        raise EditError(409, "That log was already removed.")
    edit.old_values = _snapshot(log)
    if edit.action == "delete":
        log.voided_at = func.now()
    else:
        problem = _validate_change(log, habit, edit.changes)
        if problem:
            raise EditError(422, problem)
        new = _new_values(log, habit, edit.changes)
        log.amount, log.metric = new["amount"], new["unit"]
        log.log_date = date.fromisoformat(new["log_date"])
        edit.new_values = new
        log.metadata_ = {
            **(log.metadata_ or {}),
            "edits": [*(log.metadata_ or {}).get("edits", []), edit.edit_id],
        }
    edit.status, edit.updated_at = "applied", func.now()
    db.commit()
    db.refresh(edit)
    return edit, False


def cancel(db: Session, edit_id: int) -> LogEdit:
    edit = _lock(db, edit_id)
    if edit.status in ("choosing", "pending"):
        edit.status, edit.updated_at = "cancelled", func.now()
        db.commit()
        db.refresh(edit)
    elif edit.status != "cancelled":
        raise EditError(409, _status_message(edit))
    return edit


def revert(db: Session, edit_id: int) -> tuple[LogEdit, bool]:
    """Undo an applied edit. Returns (edit, already_reverted)."""
    edit = _lock(db, edit_id)
    if edit.status == "reverted":
        return edit, True
    if edit.status != "applied":
        raise EditError(409, _status_message(edit))
    log = _lock_log(db, edit.log_id)
    if edit.action == "delete":
        if log.voided_at is None:
            raise EditError(409, "That log is already back.")
        log.voided_at = None
    else:
        if log.voided_at is not None or _snapshot(log) != edit.new_values:
            raise EditError(409, "That log changed again since, so I can't undo this change.")
        old = edit.old_values
        log.amount, log.metric = old["amount"], old["unit"]
        log.log_date = date.fromisoformat(old["log_date"])
    edit.status, edit.updated_at = "reverted", func.now()
    db.commit()
    db.refresh(edit)
    return edit, False


def _status_message(edit: LogEdit) -> str:
    return {
        "applied": "That change is already done.",
        "reverted": "That change was undone.",
        "cancelled": "That change was cancelled.",
        "superseded": "A newer change replaced this one.",
        "choosing": "Pick which log first.",
        "pending": "That change hasn't been applied yet.",
    }.get(edit.status, "That change is no longer active.")


def choices(db: Session, edit: LogEdit) -> list[dict]:
    if edit.status != "choosing":
        return []
    rows = db.execute(
        select(DailyLog, Habit)
        .join(Habit, Habit.habit_id == DailyLog.habit_id)
        .where(DailyLog.log_id.in_(edit.candidates or []), DailyLog.voided_at.is_(None))
        .order_by(DailyLog.log_date.desc(), DailyLog.log_id.desc())
    ).all()
    return [_item(log, habit) for log, habit in rows]
