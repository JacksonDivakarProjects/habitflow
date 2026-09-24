"""
Questions about the logs: the habit_logs view, dates, the template fast
path, the LLM SQL path (with retries), safe execution and the endpoints.

"Today" is frozen at Thursday 2026-09-24.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from app import querying
from app.models import DailyLog, Habit, QueryLog
from app.querying import Period, date_anchors, match_habits, match_template, parse_period

TODAY = date(2026, 9, 24)  # a Thursday
CHAT = 7


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    monkeypatch.setattr(querying, "today", lambda: TODAY)


def _habit(db, name) -> Habit:
    return db.query(Habit).filter_by(name=name).one()


def _log(db, habit: str, amount, unit: str, day: str, voided=False):
    h = _habit(db, habit)
    log = DailyLog(habit_id=h.habit_id, amount=amount, metric=unit,
                   log_date=date.fromisoformat(day), source="manual")
    db.add(log)
    db.flush()
    if voided:
        db.execute(text("UPDATE daily_logs SET voided_at = now() WHERE log_id = :i"),
                   {"i": log.log_id})
    return log


@pytest.fixture
def history(db):
    """A realistic month: mixed units, an old bare 'm', an undone log."""
    db.add(Habit(name="work", display_name="Work", metric="hours"))
    db.flush()
    _log(db, "reading", 50, "pages", "2026-08-31")        # last month
    _log(db, "reading", 20, "pages", "2026-09-01")
    _log(db, "reading", 30, "pages", "2026-09-02")
    _log(db, "reading", 30, "minutes", "2026-09-05")      # can't become pages
    _log(db, "reading", 100, "pages", "2026-09-10", voided=True)  # undone
    _log(db, "reading", 10, "pages", "2026-09-24")
    _log(db, "running", 3, "miles", "2026-09-22")
    _log(db, "running", 5, "km", "2026-09-23")
    _log(db, "running", 500, "m", "2026-09-23")           # old row: meters for a run
    _log(db, "meditation", 1, "hours", "2026-09-21")
    _log(db, "meditation", 15, "minutes", "2026-09-24")
    _log(db, "work", 6, "hours", "2026-09-15")            # last week
    _log(db, "work", 8, "hours", "2026-09-22")
    _log(db, "work", 90, "minutes", "2026-09-23")
    db.commit()


def ask(client, question):
    r = client.post("/internal/ask", json={"chat_id": CHAT, "text": question})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------
# The view
# ------------------------------------------------------------------
def test_view_hides_undone_logs_and_converts_units(db, history):
    rows = db.execute(text(
        "SELECT habit, amount, unit, habit_unit, amount_in_habit_unit, weekday_name, "
        "week_start, month_start FROM habit_logs ORDER BY log_id")).mappings().all()
    assert len(rows) == 13                                   # the undone log is gone
    by = {(r["habit"], float(r["amount"]), r["unit"]): r for r in rows}

    run_km = by[("running", 5.0, "km")]
    assert float(run_km["amount_in_habit_unit"]) == pytest.approx(3.1069, abs=1e-4)
    old_m = by[("running", 500.0, "meters")]                 # 'm' read as meters
    assert float(old_m["amount_in_habit_unit"]) == pytest.approx(0.3107, abs=1e-4)
    assert float(by[("meditation", 1.0, "hours")]["amount_in_habit_unit"]) == 60
    assert float(by[("work", 90.0, "minutes")]["amount_in_habit_unit"]) == 1.5
    assert by[("reading", 30.0, "minutes")]["amount_in_habit_unit"] is None  # not pages

    r = by[("reading", 10.0, "pages")]
    assert (r["weekday_name"], str(r["week_start"]), str(r["month_start"])) == (
        "Thursday", "2026-09-21", "2026-09-01")


def test_bare_m_is_minutes_for_time_habits(db):
    _log(db, "meditation", 20, "m", "2026-09-24")
    db.commit()
    unit, converted = db.execute(text(
        "SELECT unit, amount_in_habit_unit FROM habit_logs")).one()
    assert (unit, float(converted)) == ("minutes", 20)


def test_habit_without_a_unit(db):
    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.flush()
    _log(db, "pushups", 30, "reps", "2026-09-24")
    db.commit()
    row = db.execute(text("SELECT unit, habit_unit, amount_in_habit_unit FROM habit_logs")).one()
    assert tuple(row) == ("reps", None, None)


# ------------------------------------------------------------------
# Dates
# ------------------------------------------------------------------
@pytest.mark.parametrize("q,start,end,label", [
    ("how much did i read today", "2026-09-24", "2026-09-24", "today"),
    ("yesterday", "2026-09-23", "2026-09-23", "yesterday"),
    ("this week", "2026-09-21", "2026-09-24", "this week"),
    ("past week", "2026-09-18", "2026-09-24", "in the last 7 days"),
    ("last week", "2026-09-14", "2026-09-20", "last week"),
    ("this month", "2026-09-01", "2026-09-24", "this month"),
    ("last month", "2026-08-01", "2026-08-31", "last month"),
    ("this year", "2026-01-01", "2026-09-24", "this year"),
    ("last year", "2025-01-01", "2025-12-31", "last year"),
    ("in the last 7 days", "2026-09-18", "2026-09-24", "in the last 7 days"),
    ("past 1 day", "2026-09-24", "2026-09-24", "in the last 1 day"),
    ("last 2 weeks", "2026-09-11", "2026-09-24", "in the last 2 weeks"),
    ("last 3 months", "2026-06-25", "2026-09-24", "in the last 3 months"),
    ("in august", "2026-08-01", "2026-08-31", "in August"),
    ("in sept", "2026-09-01", "2026-09-24", "in September"),
    ("in december", "2025-12-01", "2025-12-31", "in December"),   # most recent December
    ("in may", "2026-05-01", "2026-05-31", "in May"),
])
def test_periods(q, start, end, label):
    p = parse_period(q, TODAY)
    assert (str(p.start), str(p.end), p.label) == (start, end, label)


@pytest.mark.parametrize("q,label", [
    ("how many hours did i work each week this month", "this month"),
    ("pages per week", ""),                      # a grouping, not a period
    ("my weekly average", ""),
    ("monthly totals this year", "this year"),
    ("in the past week", "in the last 7 days"),  # rolling, unlike "last week"
    ("over the past month", "in the last 30 days"),
])
def test_groupings_are_not_periods(q, label):
    assert parse_period(q, TODAY).label == label


def test_may_as_a_verb_is_not_a_month():
    assert parse_period("may i see my reading", TODAY).label == ""


def test_no_period_means_all_time():
    p = parse_period("how much have i read", TODAY)
    assert (p.start, p.end, p.where()) == (None, None, "TRUE")


def test_month_ends_and_year_boundaries():
    assert parse_period("last month", date(2026, 1, 15)).start == date(2025, 12, 1)
    assert parse_period("last 1 month", date(2026, 3, 31)).start == date(2026, 3, 1)
    assert parse_period("this week", date(2026, 9, 21)).start == date(2026, 9, 21)  # Monday


def test_anchors_are_literal_dates():
    a = date_anchors(TODAY)
    assert a["today"] == "2026-09-24" and a["today_weekday"] == "Thursday"
    assert (a["this_week_start"], a["last_week_start"], a["last_week_end"]) == (
        "2026-09-21", "2026-09-14", "2026-09-20")
    assert (a["last_month_start"], a["last_month_end"]) == ("2026-08-01", "2026-08-31")


# ------------------------------------------------------------------
# Matching habits
# ------------------------------------------------------------------
HABITS = [
    {"name": "running", "display_name": "Running"},
    {"name": "reading", "display_name": "Reading"},
    {"name": "learning_sql", "display_name": "Learning SQL"},
    {"name": "learning_concepts", "display_name": "Learning Concepts"},
    {"name": "work", "display_name": "Work"},
    {"name": "meditation", "display_name": "Meditation"},
]


@pytest.mark.parametrize("q,names", [
    ("how much i read", ["reading"]),
    ("how many hours i worked", ["work"]),
    ("how many hours did i spend working", ["work"]),
    ("how far did i run", ["running"]),
    ("how much sql this week", ["learning_sql"]),
    ("did i meditate", ["meditation"]),
    ("how much learning", []),                  # generic word alone: no habit
    ("compare reading and running", ["running", "reading"]),
])
def test_match_habits(q, names):
    assert sorted(match_habits(q, HABITS)) == sorted(names)


@pytest.mark.parametrize("q", [
    "what is my reading pattern",
    "average reading per day",
    "how much did i read each week",
    "which day do i read the most",
    "how much did i read and run",
    "how much learning",
    "show my reading",
])
def test_not_a_simple_total(q):
    assert match_template(q, HABITS, TODAY) is None


# ------------------------------------------------------------------
# Template answers (no LLM involved)
# ------------------------------------------------------------------
@pytest.mark.parametrize("question,answer", [
    ("how much i read this month", "60 pages and 30 minutes of Reading this month."),
    ("How much did I read in August?", "50 pages of Reading in August, on 1 day."),
    ("how many hours i worked", "15.5 hours of Work, on 3 days."),
    ("how many hours did I work this week", "9.5 hours of Work this week, on 2 days."),
    ("how many minutes did I work this week", "570 minutes of Work this week, on 2 days."),
    ("how many hours did i work last week", "6 hours of Work last week, on 1 day."),
    ("how far did I run this week", "6.42 miles of Running this week, on 2 days."),
    ("how many km did I run this week", "10.33 km of Running this week, on 2 days."),
    ("how long did I meditate this week", "75 minutes of Meditation this week, on 2 days."),
    ("did I meditate today", "Yes, 15 minutes of Meditation today."),
    ("did I run today", "No, there's no Running logged today."),
    ("how much sql did I do this month", "No Learning SQL logged this month."),
    ("how much sql have i done", "No Learning SQL logged yet."),
])
def test_template_answers(client, history, llm_http, question, answer):
    body = ask(client, question)
    assert body["ok"] and body["source"] == "template"
    assert body["answer"] == answer
    assert "FROM habit_logs" in body["sql"]
    assert llm_http.calls == []                          # exact and instant, even offline


def test_template_does_not_count_undone_logs(client, history):
    assert ask(client, "how much did i read on september 10")["answer"].startswith("60 pages")


# ------------------------------------------------------------------
# LLM-written SQL
# ------------------------------------------------------------------
PATTERN_SQL = (
    "SELECT weekday_name, sum(amount_in_habit_unit) AS pages FROM habit_logs "
    "WHERE habit = 'reading' AND log_date >= DATE '2026-09-01' "
    "GROUP BY weekday, weekday_name ORDER BY weekday"
)


def test_llm_query_end_to_end(client, history, llm_http):
    llm_http.queue("/query_sql", {"sql": PATTERN_SQL})
    llm_http.queue("/answer", {"answer": "You read most on Tuesdays (30 pages)."})
    body = ask(client, "what is my reading pattern this month")

    assert body["ok"] and body["source"] == "llm"
    assert body["answer"] == "You read most on Tuesdays (30 pages)."
    assert body["columns"] == ["weekday_name", "pages"]
    # Saturday's 30 minutes of reading can't be pages: NULL, as the view documents
    assert body["rows"] == [["Tuesday", 20], ["Wednesday", 30], ["Thursday", 10],
                            ["Saturday", None]]
    assert body["sql"].endswith("LIMIT 201")

    sent = llm_http.payloads("/query_sql")[0]
    assert sent["dates"]["today"] == "2026-09-24"
    assert sent["question_period"]["start"] == "2026-09-01"
    assert {h["name"] for h in sent["habits"]} >= {"reading", "work"}
    shown = llm_http.payloads("/answer")[0]
    assert shown["rows"] == body["rows"] and shown["question"].startswith("what is")


def test_rejected_sql_is_retried_with_the_reason(client, history, llm_http):
    llm_http.queue("/query_sql",
                   {"sql": "SELECT * FROM daily_logs"},
                   {"sql": "SELECT count(*) AS n FROM habit_logs WHERE habit = 'reading'"})
    llm_http.queue("/answer", {"answer": "5 reading logs."})
    body = ask(client, "how often do I read")
    assert body["ok"] and body["rows"] == [[5]]

    second = llm_http.payloads("/query_sql")[1]
    assert second["previous_sql"] == "SELECT * FROM daily_logs"
    assert "daily_logs" in second["error"]


def test_database_errors_are_retried(client, history, llm_http):
    llm_http.queue("/query_sql",
                   {"sql": "SELECT pages FROM habit_logs"},
                   {"sql": "SELECT sum(amount) AS total FROM habit_logs WHERE habit = 'reading'"})
    body = ask(client, "reading overall breakdown")
    assert body["ok"] and body["rows"] == [[140]]
    assert "database said" in llm_http.payloads("/query_sql")[1]["error"]
    assert "pages" in llm_http.payloads("/query_sql")[1]["error"]


def test_gives_up_after_three_bad_queries(client, db, history, llm_http):
    llm_http.queue("/query_sql", *[{"sql": "SELECT now()"}] * 3)
    body = ask(client, "my reading trend")
    assert not body["ok"] and "couldn't build a correct query" in body["answer"]
    assert len(llm_http.payloads("/query_sql")) == 3
    entry = db.get(QueryLog, body["query_id"])
    assert entry.attempts == 3 and "literal dates" in entry.error


def test_unanswerable_question_gets_the_llm_note(client, history, llm_http):
    llm_http.queue("/query_sql", {"sql": None, "note": "I only know your habit logs, not the weather."})
    body = ask(client, "what's the weather like?")
    assert not body["ok"] and body["answer"] == "I only know your habit logs, not the weather."


def test_llm_offline_for_a_complex_question(client, history):
    body = ask(client, "what is my reading pattern")
    assert not body["ok"] and body["source"] == "none"
    assert "offline" in body["answer"] and "how much did I read" in body["answer"]


def test_plain_answer_when_the_answer_step_is_down(client, history, llm_http):
    llm_http.queue("/query_sql", {"sql": "SELECT count(DISTINCT habit) AS habits_logged FROM habit_logs"})
    body = ask(client, "how varied are my habits")
    assert body["ok"] and body["answer"] == "Habits logged: 4."


def test_big_results_are_truncated(client, history, llm_http):
    llm_http.queue("/query_sql", {"sql":
        "SELECT d::date AS day FROM generate_series(DATE '2020-01-01', DATE '2026-09-24', "
        "INTERVAL '1 day') AS d"})
    body = ask(client, "list every day since 2020")
    assert body["ok"] and body["truncated"] and body["row_count"] == 200


def test_questions_can_mention_percent_and_colons(client, history, llm_http):
    llm_http.queue("/query_sql", {"sql":
        "SELECT '100%' AS goal, 'a:b' AS label, count(*) AS n FROM habit_logs WHERE habit LIKE 'read%'"})
    body = ask(client, "list the reading goal")
    assert body["rows"] == [["100%", "a:b", 5]]


# ------------------------------------------------------------------
# Running SQL safely (defence in depth behind the guard)
# ------------------------------------------------------------------
def test_execution_is_read_only_even_if_the_guard_were_bypassed(db, history):
    with pytest.raises(querying.QueryError, match="read-only"):
        querying.run_readonly(db, "INSERT INTO habits (name, display_name) VALUES ('x', 'X')")
    assert db.execute(text("SELECT count(*) FROM habits WHERE name = 'x'")).scalar() == 0


def test_execution_has_a_timeout(db, monkeypatch):
    monkeypatch.setattr(querying, "STATEMENT_TIMEOUT_MS", 200)
    with pytest.raises(querying.QueryError, match="timeout"):
        querying.run_readonly(db, "SELECT pg_sleep(3)")


def test_execution_leaves_the_session_usable(db, history):
    with pytest.raises(querying.QueryError):
        querying.run_readonly(db, "SELECT * FROM nope")
    assert db.execute(text("SELECT count(*) FROM habit_logs")).scalar() == 13


def test_values_are_json_friendly(db, history):
    cols, rows = querying.run_readonly(
        db, "SELECT log_date, amount, logged_at IS NOT NULL AS has_time, "
            "log_date - DATE '2026-09-01' AS days_in FROM habit_logs WHERE habit = 'work' "
            "ORDER BY log_date LIMIT 1")
    assert rows == [["2026-09-15", 6, True, 14]]


# ------------------------------------------------------------------
# Audit trail and the SQL button
# ------------------------------------------------------------------
def test_every_question_is_recorded(client, db, history):
    body = ask(client, "how much i read this month")
    entry = db.get(QueryLog, body["query_id"])
    assert (entry.chat_id, entry.source, entry.row_count) == (CHAT, "template", 2)
    assert entry.sql == body["sql"] and entry.answer == body["answer"]

    r = client.get(f"/internal/queries/{body['query_id']}")
    assert r.status_code == 200 and r.json()["sql"] == body["sql"]
    assert client.get("/internal/queries/999999").status_code == 404


def test_empty_question_is_rejected(client):
    assert client.post("/internal/ask", json={"chat_id": 1, "text": "  "}).status_code == 422


def test_no_logs_yet(client):
    body = ask(client, "how much did I run this week")
    assert body["answer"] == "No Running logged this week."


def test_answers_use_the_app_timezone_dates(client, history, llm_http):
    """The LLM gets 'today' from the app, never CURRENT_DATE."""
    llm_http.queue("/query_sql", {"sql": "SELECT count(*) AS n FROM habit_logs "
                                         "WHERE log_date = DATE '2026-09-24'"})
    ask(client, "how varied was today")
    assert llm_http.payloads("/query_sql")[0]["dates"]["yesterday"] == str(TODAY - timedelta(days=1))


def test_period_where_clause_formats():
    assert Period(date(2026, 9, 1), None, "x").where() == "log_date >= DATE '2026-09-01'"
    assert Period(None, date(2026, 9, 1), "x").where() == "log_date <= DATE '2026-09-01'"


# ------------------------------------------------------------------
# Contract with the LLM prompt: every example it learns from must pass
# the guard and run on the real view.
# ------------------------------------------------------------------
SEMANTICS = __import__("pathlib").Path(__file__).resolve().parents[2] / "llm" / "semantics.yaml"


@pytest.mark.skipif(not SEMANTICS.exists(), reason="llm/ not next to api/")
def test_prompt_examples_pass_the_guard_and_run(db, history):
    import yaml

    from app.sqlguard import check_select_sql

    examples = yaml.safe_load(SEMANTICS.read_text(encoding="utf-8"))["query"]["examples"]
    assert examples
    for ex in examples:
        error, runnable = check_select_sql(ex["sql"])
        assert error is None, (ex["question"], error)
        querying.run_readonly(db, runnable)
