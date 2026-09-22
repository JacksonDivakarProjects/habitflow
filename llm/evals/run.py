"""
Score the real LLM against evals/cases.yaml.

    cd llm && GROQ_API_KEY=... python -m evals.run            # all cases
    python -m evals.run --only "fix"                           # name filter
    python -m evals.run --min-pass 0.9                         # exit 1 below 90%

This calls the model once per case (27 requests). It is not part of
the test suite; tests/test_evals.py checks the scorer offline.
"""

import argparse
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import yaml

CASES_PATH = Path(__file__).with_name("cases.yaml")

# Enough unit normalization to compare answers; mirrors api/app/units.py.
_UNIT_ALIASES = {
    "mile": "miles", "mi": "miles", "kms": "km", "kilometer": "km", "kilometers": "km",
    "k": "km", "min": "minutes", "mins": "minutes", "minute": "minutes",
    "hr": "hours", "hrs": "hours", "hour": "hours", "page": "pages", "glass": "glasses",
    "concept": "concepts",
}


def load_cases(path: Path = CASES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _unit(value) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip().lower()
    return _UNIT_ALIASES.get(s, s) or None


def _slug(value) -> Optional[str]:
    if not value:
        return None
    return "_".join("".join(c if c.isalnum() else " " for c in str(value).lower()).split())


def _resolve_date(expected, ref: date) -> str:
    if expected == "today":
        return ref.isoformat()
    if expected == "yesterday":
        return (ref - timedelta(days=1)).isoformat()
    return (ref + timedelta(days=int(expected))).isoformat()


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def score(expect: dict, got: dict, ref: date) -> list[str]:
    """Mismatches between an expected spec and a model answer; [] = pass."""
    problems = []

    def check(field, want, have):
        if want != have:
            problems.append(f"{field}: want {want!r}, got {have!r}")

    if "habit_name" in expect:
        check("habit_name", expect["habit_name"], got.get("habit_name"))
    if "proposed_habit" in expect:
        check("proposed_habit", expect["proposed_habit"], _slug(got.get("proposed_habit")))
    if "amount" in expect:
        check("amount", float(expect["amount"]), _num(got.get("amount")))
    if "metric" in expect:
        check("metric", _unit(expect["metric"]), _unit(got.get("metric")))
    if "log_date" in expect:
        check("log_date", _resolve_date(expect["log_date"], ref), got.get("log_date"))

    extras = got.get("extra_logs") or []
    if not isinstance(extras, list):
        problems.append(f"extra_logs: not a list: {extras!r}")
        extras = []
    if "extra_logs_count" in expect:
        check("extra_logs count", expect["extra_logs_count"], len(extras))
    if "extra_logs" in expect:
        want = expect["extra_logs"]
        check("extra_logs count", len(want), len(extras))
        for i, (w, g) in enumerate(zip(want, extras, strict=False)):  # count checked above
            g = g if isinstance(g, dict) else {}
            check(f"extra_logs[{i}].habit_name", w["habit_name"], g.get("habit_name"))
            if "amount" in w:
                check(f"extra_logs[{i}].amount", float(w["amount"]), _num(g.get("amount")))
            if "metric" in w:
                check(f"extra_logs[{i}].metric", _unit(w["metric"]), _unit(g.get("metric")))
    return problems


def _draft_with_dates(draft: dict, ref: date) -> dict:
    out = dict(draft)
    when = str(out.get("log_date", ""))
    if when in ("today", "yesterday") or when.startswith("-"):
        out["log_date"] = _resolve_date(out["log_date"], ref)
    return out


def run_cases(
    extract: Callable[..., dict], data: dict, ref: date, only: Optional[str] = None
) -> list[dict]:
    """Run every case through `extract` (extract_intent's signature)."""
    results = []
    for case in data["cases"]:
        if only and only.lower() not in case["name"].lower():
            continue
        kwargs = {"habits": data["habits"]}
        if "original_text" in case:
            kwargs.update(
                user_text="",
                original_text=case["original_text"],
                clarification=case["text"],
                current_draft=_draft_with_dates(case["current_draft"], ref),
            )
        else:
            kwargs["user_text"] = case["text"]
        try:
            got = extract(**kwargs)
            problems = score(case["expect"], got, ref)
        except Exception as e:  # a crash is a failed case, not a failed run
            got, problems = None, [f"error: {e}"]
        results.append({"name": case["name"], "passed": not problems,
                        "problems": problems, "got": got})
    return results


def report(results: list[dict]) -> str:
    passed = sum(r["passed"] for r in results)
    lines = []
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        lines.append(f"{mark}  {r['name']}")
        lines += [f"        {p}" for p in r["problems"]]
    fields = Counter(p.split(":")[0].split("[")[0] for r in results for p in r["problems"])
    lines.append("")
    lines.append(f"{passed}/{len(results)} passed ({passed / max(len(results), 1):.0%})")
    if fields:
        lines.append("misses by field: " + ", ".join(f"{k} {v}" for k, v in fields.most_common()))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--only", help="run cases whose name contains this")
    parser.add_argument("--min-pass", type=float, default=0.0,
                        help="exit 1 if the pass rate is below this (0-1)")
    args = parser.parse_args(argv)

    from app.client import extract_intent  # needs GROQ_API_KEY
    from app.config import settings

    ref = datetime.now(ZoneInfo(settings.app_timezone)).date()
    results = run_cases(extract_intent, load_cases(), ref, args.only)
    print(f"model: {settings.groq_model_name}  date: {ref}\n")
    print(report(results))
    rate = sum(r["passed"] for r in results) / max(len(results), 1)
    return 0 if rate >= args.min_pass else 1


if __name__ == "__main__":
    sys.exit(main())
