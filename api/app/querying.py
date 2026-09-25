"""
Answering questions about the user's own logs ("how much did I read this
month?", "what's my reading pattern?").

Every answer comes from SQL that is shown to the user on request:

  1. Simple totals ("how much / how many hours / did I <habit> <period>") use
     a fixed template: instant, exact, and they work with the LLM offline.
  2. Anything else asks the LLM for a SELECT over the habit_logs view. The SQL
     is checked by sqlguard.check_select_sql (read-only, whitelisted tables
     and functions, row limit) and run in a READ ONLY transaction with a
     statement timeout. A rejected or failing query goes back to the LLM with
     the error, up to MAX_ATTEMPTS times.
  3. The rows are turned into a sentence (by the LLM, or plainly if it's down).

Dates are always computed here and given to the LLM as literals, so "this
month" means the same thing in the SQL as in the app's timezone.
"""

import calendar
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app import llm_client
from app.models import QueryLog
from app.parser import ALIASES as HABIT_ALIASES
from app.sqlguard import MAX_QUERY_ROWS, check_select_sql
from app.timeutil import today
from app.units import ALIASES as UNIT_ALIASES
from app.units import convert, quantity

MAX_ATTEMPTS = 3
STATEMENT_TIMEOUT_MS = 3000
ROWS_FOR_ANSWER = 50  # rows shown to the LLM when phrasing the answer

OFFLINE_ANSWER = (
    "I can't work that one out right now: the language model is offline. "
    "Simple totals still work, e.g. “how much did I read this month?”."
)


# ------------------------------------------------------------------
# Dates
# ------------------------------------------------------------------
@dataclass
class Period:
    start: Optional[date]  # None: from the first log
    end: Optional[date]  # None: up to today
    label: str  # "this month", "last 7 days", "" for all time

    def where(self, column: str = "log_date") -> str:
        if self.start and self.end:
            return f"{column} BETWEEN DATE '{self.start}' AND DATE '{self.end}'"
        if self.start:
            return f"{column} >= DATE '{self.start}'"
        if self.end:
            return f"{column} <= DATE '{self.end}'"
        return "TRUE"


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def date_anchors(ref: date) -> dict:
    """Literal dates the LLM must use instead of CURRENT_DATE."""
    week_start = ref - timedelta(days=ref.weekday())
    month_start = _month_start(ref)
    last_month_end = month_start - timedelta(days=1)
    return {
        "today": ref.isoformat(),
        "today_weekday": f"{ref:%A}",
        "yesterday": (ref - timedelta(days=1)).isoformat(),
        "this_week_start": week_start.isoformat(),
        "last_week_start": (week_start - timedelta(days=7)).isoformat(),
        "last_week_end": (week_start - timedelta(days=1)).isoformat(),
        "this_month_start": month_start.isoformat(),
        "last_month_start": _month_start(last_month_end).isoformat(),
        "last_month_end": last_month_end.isoformat(),
        "this_year_start": ref.replace(month=1, day=1).isoformat(),
        "last_7_days_start": (ref - timedelta(days=6)).isoformat(),
        "last_30_days_start": (ref - timedelta(days=29)).isoformat(),
    }


_MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
_MONTHS["sept"] = 9
_AMBIGUOUS_MONTHS = {"may", "mar"}  # "may I...", only a month after "in"
_LAST_N = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3})\s+(day|week|month|year)s?\b")


def parse_period(lowered: str, ref: date) -> Period:
    week_start = ref - timedelta(days=ref.weekday())
    m = _LAST_N.search(lowered)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if n < 1:
            n = 1
        if unit == "day":
            start = ref - timedelta(days=n - 1)
        elif unit == "week":
            start = ref - timedelta(days=7 * n - 1)
        elif unit == "month":
            start = _add_months(ref, -n) + timedelta(days=1)
        else:
            start = _add_months(ref, -12 * n) + timedelta(days=1)
        return Period(start, ref, f"in the last {n} {unit}{'s' if n > 1 else ''}")
    if re.search(r"\btoday\b|\btonight\b", lowered):
        return Period(ref, ref, "today")
    if re.search(r"\byesterday\b", lowered):
        y = ref - timedelta(days=1)
        return Period(y, y, "yesterday")
    # "past week/month" is a rolling window; "last week/month" the calendar one.
    if re.search(r"\bpast\s+week\b", lowered):
        return Period(ref - timedelta(days=6), ref, "in the last 7 days")
    if re.search(r"\bpast\s+month\b", lowered):
        return Period(ref - timedelta(days=29), ref, "in the last 30 days")
    if re.search(r"\b(last|previous)\s+week\b", lowered):
        return Period(week_start - timedelta(days=7), week_start - timedelta(days=1), "last week")
    # Only "this/current week" is a period: "each week", "per week" are a grouping.
    if re.search(r"\b(this|current)\s+week\b", lowered):
        return Period(week_start, ref, "this week")
    if re.search(r"\b(last|previous)\s+month\b", lowered):
        end = _month_start(ref) - timedelta(days=1)
        return Period(_month_start(end), end, "last month")
    if re.search(r"\b(last|previous|past)\s+year\b", lowered):
        return Period(date(ref.year - 1, 1, 1), date(ref.year - 1, 12, 31), "last year")
    if re.search(r"\b(this|current)\s+year\b|\bthis yr\b", lowered):
        return Period(ref.replace(month=1, day=1), ref, "this year")
    for word in re.findall(r"[a-z]+", lowered):
        month = _MONTHS.get(word)
        if month and (
            word not in _AMBIGUOUS_MONTHS or re.search(rf"\b(in|during) {word}\b", lowered)
        ):
            year = ref.year if month <= ref.month else ref.year - 1
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            return Period(start, min(end, ref), f"in {calendar.month_name[month]}")
    if re.search(r"\b(this|current)\s+month\b", lowered):
        return Period(_month_start(ref), ref, "this month")
    return Period(None, None, "")


# ------------------------------------------------------------------
# Habits named in a question
# ------------------------------------------------------------------
_GENERIC = {"learning", "the", "of", "and", "my", "a"}


def _stems(word: str) -> set[str]:
    stems = {word}
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            stems |= {base, base + "e"}
            if len(base) > 3 and base[-1] == base[-2]:  # running -> run
                stems.add(base[:-1])
    return stems


def match_habits(lowered: str, habits: list[dict]) -> list[str]:
    """Names of the habits a question mentions ("worked" -> work, "ran" -> running)."""
    words = re.findall(r"[a-z]+", lowered.replace("_", " "))
    stems = set().union(*(_stems(w) for w in words)) if words else set()
    found = []
    for h in habits:
        name = h["name"]
        keys = set(
            re.findall(r"[a-z]+", f"{name} {h.get('display_name', '')}".lower().replace("_", " "))
        )
        keys = {k for k in keys if k not in _GENERIC} or keys
        keys |= set(HABIT_ALIASES.get(name, []))
        if keys & stems:
            found.append(name)
    return found


def _asked_unit(lowered: str) -> Optional[str]:
    m = re.search(r"\bhow (?:many|much)\s+([a-z]+)", lowered)
    if m and m.group(1) in UNIT_ALIASES:
        return UNIT_ALIASES[m.group(1)]
    m = re.search(r"\bin\s+([a-z]+)\b", lowered)
    if m and m.group(1) in UNIT_ALIASES:
        return UNIT_ALIASES[m.group(1)]
    return None


# ------------------------------------------------------------------
# Template: totals for one habit over one period
# ------------------------------------------------------------------
_NOT_A_TOTAL = re.compile(
    r"\b(pattern|patterns|trend|trends|per|each|every|daily|weekly|monthly|average|avg|mean|"
    r"compare|vs|versus|most|least|best|worst|streak|streaks|which|when|breakdown|history|"
    r"list|show|by day|by week|by month|longest|shortest|than|and|or|between|consisten\w*)\b"
)
_TOTAL = re.compile(r"\b(how (much|many|long|far)|total|(did|have) (i|my))\b")


@dataclass
class Template:
    habit: dict
    period: Period
    unit: Optional[str]
    yes_no: bool
    sql: str


def match_template(question: str, habits: list[dict], ref: date) -> Optional[Template]:
    lowered = " ".join(question.lower().split())
    if not _TOTAL.search(lowered) or _NOT_A_TOTAL.search(lowered):
        return None
    names = match_habits(lowered, habits)
    if len(names) != 1:
        return None
    habit = next(h for h in habits if h["name"] == names[0])
    period = parse_period(lowered, ref)
    name = habit["name"].replace("'", "''")
    where = f"habit = '{name}' AND {period.where()}"
    sql = (
        "SELECT CASE WHEN amount_in_habit_unit IS NULL THEN unit ELSE habit_unit END AS unit,\n"
        "       round(sum(coalesce(amount_in_habit_unit, amount)), 2) AS total,\n"
        "       count(DISTINCT log_date) AS days,\n"
        "       count(*) AS logs\n"
        "FROM habit_logs\n"
        f"WHERE {where}\n"
        "GROUP BY 1\n"
        "ORDER BY total DESC"
    )
    return Template(
        habit=habit,
        period=period,
        unit=_asked_unit(lowered),
        yes_no=bool(re.match(r"^(did|have) (i|my)\b", lowered)),
        sql=sql,
    )


def _num(x: float) -> str:
    return f"{round(x, 2):g}"


def template_answer(t: Template, rows: list[dict]) -> str:
    display = t.habit.get("display_name") or t.habit["name"]
    when = f" {t.period.label}" if t.period.label else ""
    if not rows:
        if t.yes_no:
            return f"No, there's no {display} logged{when}."
        return f"No {display} logged{when} yet." if not when else f"No {display} logged{when}."
    parts = []
    for r in rows:
        amount, unit = float(r["total"]), r["unit"]
        if t.unit and unit and t.unit != unit:
            converted = convert(amount, unit, t.unit)
            if converted is not None:
                amount, unit = converted, t.unit
        parts.append(quantity(round(amount, 2), unit) if unit else _num(amount))
    days = max(int(r["days"]) for r in rows) if len(rows) == 1 else None
    total = " and ".join(parts)
    lead = "Yes, " if t.yes_no else ""
    text = f"{lead}{total} of {display}{when}"
    if days is not None and (t.period.start != t.period.end or t.period.start is None):
        text += f", on {days} day{'s' if days != 1 else ''}"
    return text[0].upper() + text[1:] + "."


# ------------------------------------------------------------------
# Running SQL
# ------------------------------------------------------------------
class QueryError(Exception):
    pass


def _json_value(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def run_readonly(db: Session, sql: str) -> tuple[list[str], list[list]]:
    """Run a checked SELECT on its own connection, in a READ ONLY transaction
    with a statement timeout. Raw psycopg, so '%' and ':' in the SQL are literal."""
    engine = db.get_bind()
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        try:
            with raw.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                cur.execute(sql)
                columns = [d.name for d in cur.description or []]
                rows = [[_json_value(v) for v in row] for row in cur.fetchall()]
        except Exception as e:
            message = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            raise QueryError(message) from e
        finally:
            raw.rollback()
    return columns, rows


# ------------------------------------------------------------------
# Asking
# ------------------------------------------------------------------
@dataclass
class Answer:
    ok: bool
    answer: str
    source: str  # template | llm | none
    sql: Optional[str] = None
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    truncated: bool = False
    attempts: int = 0
    error: Optional[str] = None
    query_id: Optional[int] = None

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _records(columns: list[str], rows: list[list]) -> list[dict]:
    return [dict(zip(columns, r, strict=False)) for r in rows]


def _fmt(v) -> str:
    if isinstance(v, float):
        return _num(v)
    return "—" if v is None else str(v)


def plain_answer(columns: list[str], rows: list[list], truncated: bool) -> str:
    """An answer without the LLM: the value itself, or a pointer to the table."""
    if not rows:
        return "Nothing matches that: no logs found."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0].replace('_', ' ').capitalize()}: {_fmt(rows[0][0])}."
    if len(rows) == 1:
        pairs = zip(columns, rows[0], strict=False)
        return "; ".join(f"{c.replace('_', ' ')}: {_fmt(v)}" for c, v in pairs) + "."
    more = f" (first {len(rows)})" if truncated else ""
    return f"Here's what I found{more}:"


def _llm_sql(
    question: str, habits: list[dict], anchors: dict, period: Period, db: Session
) -> Answer:
    previous_sql, error = None, None
    last_error = "no SQL produced"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            reply = llm_client.post(
                "/query_sql",
                {
                    "question": question,
                    "habits": habits,
                    "dates": anchors,
                    "question_period": (
                        {
                            "start": period.start and period.start.isoformat(),
                            "end": period.end and period.end.isoformat(),
                            "label": period.label,
                        }
                        if period.label
                        else None
                    ),
                    "previous_sql": previous_sql,
                    "error": error,
                },
            )
        except llm_client.LLMUnavailable as e:
            return Answer(False, OFFLINE_ANSWER, "none", attempts=attempt - 1, error=str(e))

        sql = reply.get("sql")
        if not sql:
            note = reply.get("note") or (
                "I can only answer questions about your logged habits, "
                "e.g. “how many hours did I work this week?”."
            )
            return Answer(False, note, "llm", attempts=attempt, error="no sql: " + note)

        problem, runnable = check_select_sql(sql)
        if problem is None:
            try:
                columns, rows = run_readonly(db, runnable)
            except QueryError as e:
                problem = f"the database said: {e}"
            else:
                truncated = len(rows) > MAX_QUERY_ROWS
                return Answer(
                    True,
                    "",
                    "llm",
                    sql=runnable,
                    columns=columns,
                    rows=rows[:MAX_QUERY_ROWS],
                    truncated=truncated,
                    attempts=attempt,
                )
        previous_sql, error, last_error = sql, problem, problem
    return Answer(
        False,
        "Sorry, I couldn't build a correct query for that. Try asking it another way, "
        "e.g. “how much did I read each week this month?”.",
        "llm",
        sql=previous_sql,
        attempts=MAX_ATTEMPTS,
        error=last_error,
    )


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def readable_rows(rows: list[list]) -> list[list]:
    """Rows for the LLM to phrase, with dates as people say them ("Mon 7 Sep"),
    so they don't end up as "2026-09-07" in the answer."""
    def cell(v):
        if isinstance(v, str) and _ISO_DATE.fullmatch(v):
            d = date.fromisoformat(v)
            return f"{d:%a} {d.day} {d:%b}" + (f" {d.year}" if d.year != today().year else "")
        return v

    return [[cell(v) for v in r] for r in rows]


def _phrase(question: str, result: Answer, anchors: dict) -> str:
    try:
        reply = llm_client.post(
            "/answer",
            {
                "question": question,
                "sql": result.sql,
                "columns": result.columns,
                "rows": readable_rows(result.rows[:ROWS_FOR_ANSWER]),
                "row_count": result.row_count,
                "truncated": result.truncated,
                "dates": anchors,
            },
        )
        text = (reply.get("answer") or "").strip()
        if text:
            return text
    except llm_client.LLMUnavailable:
        pass
    return plain_answer(result.columns, result.rows, result.truncated)


def ask(question: str, chat_id: int, db: Session, habits: list[dict]) -> Answer:
    started = time.monotonic()
    ref = today()
    anchors = date_anchors(ref)

    template = match_template(question, habits, ref)
    if template:
        problem, runnable = check_select_sql(template.sql)
        assert problem is None, problem  # our own template must always pass
        try:
            columns, rows = run_readonly(db, runnable)
        except QueryError as e:
            result = Answer(
                False,
                "Sorry, that query failed. Please try again.",
                "template",
                sql=runnable,
                attempts=1,
                error=str(e),
            )
        else:
            result = Answer(
                True,
                template_answer(template, _records(columns, rows)),
                "template",
                sql=runnable,
                columns=columns,
                rows=rows,
                attempts=1,
            )
    else:
        period = parse_period(" ".join(question.lower().split()), ref)
        result = _llm_sql(question, habits, anchors, period, db)
        if result.ok:
            result.answer = _phrase(question, result, anchors)

    entry = QueryLog(
        chat_id=chat_id,
        question=question,
        sql=result.sql,
        source=result.source,
        row_count=result.row_count if result.ok else None,
        answer=result.answer,
        error=result.error,
        attempts=max(result.attempts, 1),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    db.add(entry)
    db.commit()
    result.query_id = entry.query_id
    return result
