"""The eval harness itself, offline: case file shape and the scorer."""

from datetime import date

import pytest

from evals.run import _resolve_date, load_cases, report, run_cases, score

REF = date(2026, 9, 23)
KNOWN_FIELDS = {
    "habit_name", "proposed_habit", "amount", "metric", "suggested_metric", "log_date",
    "extra_logs", "extra_logs_count",
}


@pytest.fixture(scope="module")
def data():
    return load_cases()


def test_case_file_is_well_formed(data):
    names = [c["name"] for c in data["cases"]]
    assert len(names) == len(set(names)), "case names must be unique"
    habit_names = {h["name"] for h in data["habits"]}
    for case in data["cases"]:
        assert case["text"] and case["expect"], case["name"]
        assert set(case["expect"]) <= KNOWN_FIELDS, case["name"]
        if "habit_name" in case["expect"]:
            assert case["expect"]["habit_name"] in habit_names, case["name"]
        if "original_text" in case:
            assert "current_draft" in case, case["name"]


def test_case_file_covers_every_flow(data):
    cases = data["cases"]
    assert sum("original_text" in c for c in cases) >= 5            # corrections
    assert sum("proposed_habit" in c["expect"] for c in cases) >= 3  # new habits
    assert sum("extra_logs" in c["expect"] or "extra_logs_count" in c["expect"]
               for c in cases) >= 3                                  # multi-habit
    assert sum("log_date" in c["expect"] and c["expect"]["log_date"] != "today"
               for c in cases) >= 3                                  # relative dates


@pytest.mark.parametrize(
    "expected, iso",
    [("today", "2026-09-23"), ("yesterday", "2026-09-22"), (-2, "2026-09-21")],
)
def test_resolve_date(expected, iso):
    assert _resolve_date(expected, REF) == iso


def test_score_pass_with_normalized_units_and_numbers():
    got = {"habit_name": "running", "amount": "4", "metric": "Mi", "log_date": "2026-09-23"}
    expect = {"habit_name": "running", "amount": 4, "metric": "miles", "log_date": "today"}
    assert score(expect, got, REF) == []


def test_score_reports_each_mismatch():
    got = {"habit_name": "reading", "amount": 5, "metric": None, "log_date": "2026-09-22"}
    expect = {"habit_name": "running", "amount": 4, "metric": "miles", "log_date": "today"}
    assert score(expect, got, REF) == [
        "habit_name: want 'running', got 'reading'",
        "amount: want 4.0, got 5.0",
        "metric: want 'miles', got None",
        "log_date: want '2026-09-23', got '2026-09-22'",
    ]


def test_score_null_metric_means_do_not_invent_one():
    assert score({"metric": None}, {"metric": None, "suggested_metric": "pages"}, REF) == []
    assert score({"metric": None}, {"metric": "pages"}, REF) != []


def test_score_proposed_habit_is_slugged():
    assert score({"proposed_habit": "learning_rust"}, {"proposed_habit": "Learning Rust"}, REF) == []


def test_score_extra_logs():
    expect = {"extra_logs": [{"habit_name": "reading", "amount": 20, "metric": "pages"}]}
    good = {"extra_logs": [{"habit_name": "reading", "amount": 20, "metric": "page"}]}
    assert score(expect, good, REF) == []
    assert score(expect, {"extra_logs": []}, REF) == ["extra_logs count: want 1, got 0"]
    assert score({"extra_logs_count": 2}, {"extra_logs": "nope"}, REF)[0].startswith(
        "extra_logs: not a list"
    )


def test_run_cases_with_a_perfect_model(data):
    """A fake model that answers each case exactly as expected scores 100%."""
    by_text = {}
    for case in data["cases"]:
        e = case["expect"]
        answer = {k: e[k] for k in ("habit_name", "proposed_habit", "amount", "metric",
                                    "suggested_metric") if k in e}
        if "log_date" in e:
            answer["log_date"] = _resolve_date(e["log_date"], REF)
        n = e.get("extra_logs_count", len(e.get("extra_logs", [])))
        answer["extra_logs"] = e.get("extra_logs") or [{"habit_name": "x"}] * n
        by_text[case["text"]] = answer

    def perfect(**kwargs):
        return by_text[kwargs.get("clarification") or kwargs["user_text"]]

    results = run_cases(perfect, data, REF)
    assert all(r["passed"] for r in results), report(results)


def test_run_cases_counts_crashes_as_failures(data):
    def broken(**kwargs):
        raise RuntimeError("rate limited")

    results = run_cases(broken, data, REF, only="basic")
    assert results == [{"name": "basic", "passed": False,
                        "problems": ["error: rate limited"], "got": None}]
    assert "0/1 passed" in report(results)


def test_correction_cases_pass_the_draft_and_resolve_its_dates(data):
    seen = []

    def spy(**kwargs):
        seen.append(kwargs)
        return {}

    run_cases(spy, data, REF, only="fix date")
    assert seen[0]["original_text"] == "read 20 pages"
    assert seen[0]["clarification"] == "it was yesterday"
    assert seen[0]["current_draft"]["log_date"] == "2026-09-23"


def test_score_suggested_metric():
    assert score({"suggested_metric": "reps"}, {"metric": None, "suggested_metric": "rep"}, REF) == []
    assert score({"suggested_metric": "reps"}, {"suggested_metric": None}, REF) == [
        "suggested_metric: want 'reps', got None"
    ]


def test_case_file_covers_unit_decisions(data):
    names = {c["name"] for c in data["cases"]}
    assert {"m is meters for a run", "m is minutes for meditation", "no conversion",
            "new habit gets natural unit"} <= names


# ------------------------------------------------------------------
# Question evals (evals/questions.py)
# ------------------------------------------------------------------
from evals import questions as qevals  # noqa: E402


def test_question_case_file_is_well_formed():
    data = qevals.load_cases()
    assert {c["kind"] for c in data["classify"]} == {"log", "query", "chat"}
    names = [c["name"] for c in data["query"]]
    assert len(names) == len(set(names))
    anchors = qevals.anchors(REF)
    for case in data["query"]:
        assert case["question"]
        assert case.get("refuse") or case.get("must"), case["name"]
        if "period" in case:
            assert case["period"] in anchors, case["name"]


def test_question_anchors_match_the_api():
    a = qevals.anchors(date(2026, 9, 24))
    assert (a["this_week_start"], a["last_week_start"], a["last_week_end"]) == (
        "2026-09-21", "2026-09-14", "2026-09-20")
    assert (a["this_month_start"], a["last_month_start"]) == ("2026-09-01", "2026-08-01")


def test_query_scorer():
    dates = qevals.anchors(REF)
    case = {"name": "x", "must": ["habit = 'work'"], "period": "this_month_start"}
    good = {"sql": f"SELECT sum(amount) FROM habit_logs WHERE habit='work' AND "
                   f"log_date >= DATE '{dates['this_month_start']}'"}
    assert qevals.score_query({**case, "must": ["habit='work'"]}, good, dates) == []
    bad = {"sql": "SELECT * FROM daily_logs WHERE log_date >= CURRENT_DATE"}
    problems = qevals.score_query(case, bad, dates)
    assert "missing: habit = 'work'" in problems
    assert "forbidden: current_date" in problems and "forbidden: daily_logs" in problems
    assert any(p.startswith("period:") for p in problems)
    assert qevals.score_query({"refuse": True}, {"sql": None}, dates) == []
    assert qevals.score_query({"refuse": True}, {"sql": "SELECT 1"}, dates)
    assert qevals.score_query(case, {"sql": None, "note": "no"}, dates) == ["no SQL (note: 'no')"]


def test_query_scorer_uses_the_guard():
    guard = lambda sql: ("only SELECT", "")  # noqa: E731
    problems = qevals.score_query({"must": []}, {"sql": "SELECT 1"}, qevals.anchors(REF), guard)
    assert problems == ["guard: only SELECT"]


def test_question_run_cases_with_a_fake_model():
    data = qevals.load_cases()
    results = qevals.run_cases(
        classify=lambda text, habits: {"kind": "query"},
        query_sql=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
        data=data, ref=REF)
    by_name = {r["name"]: r for r in results}
    assert by_name["classify: read today"]["passed"]
    assert not by_name["classify: tell me a joke"]["passed"]
    assert by_name["query: reading pattern"]["problems"] == ["error: boom"]


def test_api_guard_loads_next_to_llm_app():
    pytest.importorskip("sqlglot")
    guard = qevals._api_guard()
    assert guard is not None
    assert guard("SELECT * FROM daily_logs")[0]
    assert guard("SELECT count(*) FROM habit_logs")[0] is None
