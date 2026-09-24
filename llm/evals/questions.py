"""
Score the real LLM on questions: routing and text-to-SQL (evals/question_cases.yaml).

    cd llm && GROQ_API_KEY=... python -m evals.questions          # all cases
    python -m evals.questions --only streak                        # name filter
    python -m evals.questions --min-pass 0.9                       # exit 1 below 90%

When the api/ package sits next to llm/ (a repo checkout), every SQL is also
run through the API's guard, exactly as production would.
It is not part of the test suite; tests/test_evals.py checks the scorer offline.
"""

import argparse
import importlib.util
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import yaml

from evals.run import report

CASES_PATH = Path(__file__).with_name("question_cases.yaml")
ALWAYS_FORBIDDEN = ["current_date", "now()", "current_timestamp", "daily_logs"]


def load_cases(path: Path = CASES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def anchors(ref: date) -> dict:
    """The same literal dates the API sends (api/app/querying.py: date_anchors)."""
    week_start = ref - timedelta(days=ref.weekday())
    month_start = ref.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return {
        "today": ref.isoformat(),
        "today_weekday": f"{ref:%A}",
        "yesterday": (ref - timedelta(days=1)).isoformat(),
        "this_week_start": week_start.isoformat(),
        "last_week_start": (week_start - timedelta(days=7)).isoformat(),
        "last_week_end": (week_start - timedelta(days=1)).isoformat(),
        "this_month_start": month_start.isoformat(),
        "last_month_start": last_month_end.replace(day=1).isoformat(),
        "last_month_end": last_month_end.isoformat(),
        "this_year_start": ref.replace(month=1, day=1).isoformat(),
        "last_7_days_start": (ref - timedelta(days=6)).isoformat(),
        "last_30_days_start": (ref - timedelta(days=29)).isoformat(),
    }


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.lower())


def _api_guard() -> Optional[Callable[[str], tuple]]:
    """api/app/sqlguard.py's check_select_sql, loaded by path (llm/ has its own
    `app` package), or None without a repo checkout or sqlglot installed."""
    path = Path(__file__).resolve().parents[2] / "api" / "app" / "sqlguard.py"
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("habitflow_api_sqlguard", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module.check_select_sql


def score_query(case: dict, reply: dict, dates: dict, guard=None) -> list[str]:
    sql = reply.get("sql")
    if case.get("refuse"):
        return [] if not sql else [f"should refuse, got SQL: {sql[:80]}"]
    if not sql:
        return [f"no SQL (note: {reply.get('note')!r})"]
    problems = []
    text = _norm(sql)
    for s in case.get("must", []):
        if _norm(s) not in text:
            problems.append(f"missing: {s}")
    for s in case.get("must_not", []) + ALWAYS_FORBIDDEN:
        if _norm(s) in text:
            problems.append(f"forbidden: {s}")
    if case.get("period") and dates[case["period"]] not in sql:
        problems.append(f"period: {case['period']} ({dates[case['period']]}) not used")
    if guard:
        error, _ = guard(sql)
        if error:
            problems.append(f"guard: {error}")
    return problems


def run_cases(classify, query_sql, data: dict, ref: date, only: Optional[str] = None,
              guard=None) -> list[dict]:
    dates = anchors(ref)
    results = []
    for case in data["classify"]:
        name = f"classify: {case['text']}"
        if only and only.lower() not in name.lower():
            continue
        try:
            got = classify(case["text"], data["habits"])
            problems = [] if got.get("kind") == case["kind"] else [
                f"kind: want {case['kind']!r}, got {got.get('kind')!r}"]
        except Exception as e:
            got, problems = None, [f"error: {e}"]
        results.append({"name": name, "passed": not problems, "problems": problems, "got": got})

    for case in data["query"]:
        name = f"query: {case['name']}"
        if only and only.lower() not in name.lower():
            continue
        try:
            got = query_sql(question=case["question"], habits=data["habits"], dates=dates)
            problems = score_query(case, got, dates, guard)
        except Exception as e:
            got, problems = None, [f"error: {e}"]
        results.append({"name": name, "passed": not problems, "problems": problems, "got": got})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--only", help="run cases whose name contains this")
    parser.add_argument("--min-pass", type=float, default=0.0)
    args = parser.parse_args(argv)

    from app import questions  # needs GROQ_API_KEY
    from app.config import settings

    ref = datetime.now(ZoneInfo(settings.app_timezone)).date()
    guard = _api_guard()
    results = run_cases(questions.classify, questions.query_sql, load_cases(), ref,
                        args.only, guard)
    print(f"model: {settings.groq_model_name}  date: {ref}  "
          f"api guard: {'on' if guard else 'off'}\n")
    print(report(results))
    rate = sum(r["passed"] for r in results) / max(len(results), 1)
    return 0 if rate >= args.min_pass else 1


if __name__ == "__main__":
    sys.exit(main())
