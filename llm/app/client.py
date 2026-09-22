import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from openai import OpenAI

from app.config import settings

SEMANTICS = yaml.safe_load(
    (Path(__file__).parent.parent / "semantics.yaml").read_text()
)

client = OpenAI(
    base_url=settings.groq_base_url,
    api_key=settings.groq_api_key,
)


def _build_system_prompt(habits: list[dict]) -> str:
    if habits:
        lines = []
        for h in habits:
            default = h.get("default_metric") or "none"
            recent = h.get("recent_metric") or "none"
            lines.append(
                f"- {h['name']} (display: {h['display_name']}, "
                f"default_metric: {default}, recently_used: {recent})"
            )
        habits_str = "\n".join(lines)
    else:
        habits_str = "(none yet — the user hasn't created any)"

    schema_lines = []
    for t in SEMANTICS.get("tables", []):
        schema_lines.append(f"\nTABLE {t['name']}: {t['description']}")
        for c in t["columns"]:
            bits = [f"  - {c['name']} {c['type']}"]
            if c.get("primary_key"):
                bits.append("PRIMARY KEY")
            if c.get("foreign_key"):
                bits.append(f"REFERENCES {c['foreign_key']}")
            if c.get("nullable"):
                bits.append("NULLABLE")
            if c.get("description"):
                bits.append(f"-- {c['description']}")
            schema_lines.append(" ".join(bits))
    schema_str = "\n".join(schema_lines)

    rules_str = "\n".join(f"- {r}" for r in SEMANTICS.get("rules", []))

    sql_examples = SEMANTICS.get("sql_examples", [])
    sql_examples_str = "\n".join(
        f"Intent: {ex['intent']}\nSQL: {ex['draft_sql']}"
        for ex in sql_examples
    )

    shape = json.dumps(SEMANTICS.get("intent_shape", {}), indent=2)
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    return f"""You are a Text-to-Intent + SQL engine for a personal habit tracker.

Today: {today}
Yesterday: {yesterday}

DATABASE SCHEMA:
{schema_str}

KNOWN HABITS (the ONLY valid values for habit_name):
{habits_str}

RULES:
{rules_str}

SQL EXAMPLES:
{sql_examples_str}

Return ONLY a valid JSON object with this shape:
{shape}

No markdown. No explanation. Just the JSON."""


def extract_intent(
    user_text: str,
    habits: list[dict],
    previous_intent: Optional[dict] = None,
    previous_error: Optional[str] = None,
    clarification: Optional[str] = None,
    original_text: Optional[str] = None,
) -> dict:
    messages = [{"role": "system", "content": _build_system_prompt(habits)}]

    if clarification and original_text:
        messages.append({"role": "user", "content": original_text})
        messages.append({
            "role": "assistant",
            "content": json.dumps({"question": "clarification needed"}),
        })
        messages.append({"role": "user", "content": clarification})
    else:
        messages.append({"role": "user", "content": user_text})

    if previous_intent and previous_error:
        messages.append({"role": "assistant", "content": json.dumps(previous_intent)})
        messages.append({
            "role": "user",
            "content": (
                f"Your draft_sql failed the dry-run with this error:\n"
                f"{previous_error}\n\n"
                f"Analyze the error, correct the SQL, and return the corrected JSON."
            ),
        })

    response = client.chat.completions.create(
        model=settings.groq_model_name,
        messages=messages,
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)