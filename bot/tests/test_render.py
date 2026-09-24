"""Pure rendering: API data in, HTML text + buttons out."""

import httpx
import pytest

import bot
from tests.conftest import buttons

ITEM = {"habit": "Running", "amount": 4.0, "unit": "miles", "quantity": "4 miles",
        "log_date": "2026-09-24", "when": "today"}
CARD = {"audit_id": 7, "status": "pending", "preview": "4 miles of Running today",
        "items": [ITEM], "metric_source": "explicit", "draft_sql": "INSERT ...",
        "offline": False, "skipped": []}
SUMMARY = {"log_id": 3, "habit": "Running", "amount": 4.0, "metric": "miles",
           "log_date": "2026-09-24", "when": "today", "streak_days": 3, "week_total": 12.0}


# ------------------------------------------------------------------
# Draft cards
# ------------------------------------------------------------------
def test_card():
    text, markup = bot.render_card(CARD)
    assert text == "📝 <b>Log this?</b>\n• <b>Running</b> · 4 miles · today"
    assert buttons(markup) == [("✅ Save", "save:7"), ("✏️ Change", "change:7"),
                               ("✖ Cancel", "cancel:7"), ("🔍 SQL", "sql:7")]


def test_card_without_sql_button():
    assert ("🔍 SQL", "sql:7") not in buttons(bot.card_markup(7, sql_button=False))


def test_multi_log_card_has_a_line_each():
    card = {**CARD, "items": [ITEM, {**ITEM, "habit": "Reading", "quantity": "20 pages"}]}
    text, _ = bot.render_card(card)
    assert text.splitlines()[1:] == ["• <b>Running</b> · 4 miles · today",
                                     "• <b>Reading</b> · 20 pages · today"]


def test_card_notes():
    text, _ = bot.render_card({**CARD, "metric_source": "suggested", "offline": True,
                               "skipped": ["Pushups (new habit, send it on its own)"]})
    assert "Unit guessed" in text
    assert "⚠️ Not included: Pushups (new habit, send it on its own)" in text
    assert "AI is offline" in text


def test_user_text_is_escaped():
    text, _ = bot.render_card({**CARD, "items": [{**ITEM, "habit": "<b>x</b> & y"}]})
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_card_from_an_older_api_without_items():
    text, _ = bot.render_card({"audit_id": 7, "preview": "4 miles of Running today"})
    assert "• 4 miles of Running today" in text


def test_unit_question_offers_buttons():
    text, markup = bot.render_unit_question({
        "audit_id": 7, "needs_input": "unit", "prompt": "Got it: 4 of Running today. What unit?",
        "unit_options": ["miles", "km", "meters", "minutes", "hours"]})
    assert text.startswith("❓ Got it: 4 of Running today. What unit?")
    assert buttons(markup) == [
        ("miles", "unit:7:miles"), ("km", "unit:7:km"), ("meters", "unit:7:meters"),
        ("minutes", "unit:7:minutes"), ("hours", "unit:7:hours"), ("✖ Cancel", "cancel:7")]


def test_new_habit_question():
    text, markup = bot.render_habit_question({"audit_id": 7, "prompt": "“Pushups” is new."})
    assert text == "✨ “Pushups” is new."
    assert buttons(markup) == [("✨ Create habit", "create:7"), ("✖ No", "nocreate:7")]


def test_render_draft_dispatches():
    assert bot.render_draft({**CARD, "needs_input": "unit", "prompt": "?"})[0].startswith("❓")
    assert bot.render_draft({**CARD, "needs_input": "habit", "prompt": "?"})[0].startswith("✨")
    assert bot.render_draft(CARD)[0].startswith("📝")


def test_change_prompt_quotes_the_card_and_offers_quick_fixes():
    text, markup = bot.render_change_prompt(7, "📝 Log this?\n• Running · 4 miles · today")
    assert "• Running · 4 miles · today" in text and "What should change?" in text
    assert buttons(markup) == [("📅 It was yesterday", "fix:7:yesterday"),
                               ("📅 It was today", "fix:7:today"), ("↩️ Keep as is", "keep:7")]


# ------------------------------------------------------------------
# Saved
# ------------------------------------------------------------------
@pytest.mark.parametrize("streak,progress", [
    (3, "   🔥 3-day streak · 12 miles this week"),
    (1, "   🌱 day 1 of a new streak · 12 miles this week"),
    (0, "   12 miles this week"),
])
def test_saved(streak, progress):
    text, markup = bot.render_saved({"log_id": 3, "log_ids": [3],
                                     "summaries": [{**SUMMARY, "streak_days": streak}]})
    assert text.splitlines() == ["✅ <b>Saved</b>", "• <b>Running</b> · 4 miles · today", progress]
    assert buttons(markup) == [("↩️ Undo", "undo:3")]


def test_saved_several_has_one_undo_for_all():
    text, markup = bot.render_saved({
        "audit_id": 7, "log_id": 3, "log_ids": [3, 4],
        "summaries": [SUMMARY, {**SUMMARY, "habit": "Reading", "amount": 1, "metric": "pages",
                                "week_total": 1}]})
    assert "• <b>Reading</b> · 1 page · today" in text
    assert buttons(markup) == [("↩️ Undo", "undo_audit:7")]


def test_quantities_read_naturally():
    assert bot._qty(1, "miles") == "1 mile"
    assert bot._qty(1.0, "glasses") == "1 glass"
    assert bot._qty(2.5, "hours") == "2.5 hours"
    assert bot._qty(3, "burpees") == "3 burpees"


# ------------------------------------------------------------------
# Answers
# ------------------------------------------------------------------
def test_template_answer_is_just_the_sentence():
    text, markup = bot.render_answer({
        "ok": True, "source": "template", "answer": "60 pages of Reading this month.",
        "columns": ["unit", "total"], "rows": [["pages", 60], ["minutes", 30]],
        "sql": "SELECT ...", "query_id": 9})
    assert text == "💬 60 pages of Reading this month."
    assert buttons(markup) == [("🔍 SQL", "qsql:9")]


def test_llm_answer_with_rows_gets_a_table():
    text, _ = bot.render_answer({
        "ok": True, "source": "llm", "answer": "You read most on Wednesdays.",
        "columns": ["weekday_name", "pages"],
        "rows": [["Tuesday", 20], ["Wednesday", 30.5], ["Saturday", None]],
        "sql": "SELECT ...", "query_id": 9})
    assert text.startswith("💬 You read most on Wednesdays.\n<pre>")
    table = text.split("<pre>")[1].split("</pre>")[0]
    assert table.splitlines() == [
        "weekday name  pages",
        "────────────  ─────",
        "Tuesday          20",
        "Wednesday      30.5",
        "Saturday          —",
    ]


def test_single_row_answer_has_no_table():
    text, _ = bot.render_answer({"ok": True, "source": "llm", "answer": "5 logs.",
                                 "columns": ["n"], "rows": [[5]], "sql": "S", "query_id": 1})
    assert "<pre>" not in text


def test_failed_answer():
    text, markup = bot.render_answer({"ok": False, "source": "none", "answer": "LLM offline.",
                                      "rows": [], "sql": None, "query_id": 4})
    assert text == "🤔 LLM offline." and markup is None


def test_table_dates_long_cells_and_limits(monkeypatch):
    monkeypatch.setattr(bot, "TABLE_MAX_ROWS", 2)
    monkeypatch.setattr(bot, "TABLE_MAX_COLS", 2)
    table = bot.format_table(
        ["week_start", "a_really_long_column_name", "extra"],
        [["2026-09-21", "x" * 40, 1], ["2026-09-14", "y", 2], ["2026-09-07", "z", 3]])
    lines = table.splitlines()
    assert lines[0].startswith("week start  a really long c…")
    assert lines[2].startswith("Mon 21 Sep  xxxxxxxxxxxxxxx…")
    assert lines[-2:] == ["… and 1 more", "(1 more columns not shown)"]


def test_table_booleans_and_html_escaping():
    text, _ = bot.render_answer({"ok": True, "source": "llm", "answer": "a < b",
                                 "columns": ["x"], "rows": [[True], ["<i>"]]})
    assert "a &lt; b" in text and "&lt;i&gt;" in text and "yes" in text


def test_sql_message():
    assert bot.render_sql("SQL used", "  SELECT 1 < 2\n") == (
        "🔍 <b>SQL used</b>\n<pre>SELECT 1 &lt; 2</pre>")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
@pytest.mark.parametrize("status,detail,expected", [
    (404, "Audit not found", "I can't find that anymore"),
    (409, "Audit status is superseded", "replaced by a newer one"),
    (409, "Audit status is executed", "already saved"),
    (409, "Audit status is cancelled", "was cancelled"),
    (409, "Audit status is failed", "no longer active"),
    (422, "Loop 1 failed", "couldn't turn that into a log"),
    (500, "boom", "went wrong on my side"),
])
def test_friendly_error(status, detail, expected):
    assert expected in bot._friendly_error(httpx.Response(status, json={"detail": detail}))


def test_friendly_error_non_json_body():
    assert "went wrong" in bot._friendly_error(httpx.Response(502, text="<html>bad gateway"))


@pytest.mark.parametrize("text,is_q", [
    ("how much did I read?", True), ("what's my streak", True), ("did I run today", True),
    ("reading this month?", True), ("km", False), ("6 miles, not 4", False),
    ("it was yesterday", False), ("minutes", False), ("km?", False), ("hours ?", True),
])
def test_looks_like_question(text, is_q):
    assert bot._looks_like_question(text) is is_q


def test_help_covers_logging_questions_and_every_command():
    for name, _ in bot.COMMANDS:
        assert f"/{name}" in bot.HELP_TEXT
    assert "how much did I read this month?" in bot.HELP_TEXT
    assert "remind" not in bot.HELP_TEXT.lower()


def test_legacy_buttons_map_to_new_actions():
    assert bot._parse_callback("approve:7") == ("save", 7, None)
    assert bot._parse_callback("feedback:7") == ("change", 7, None)
    assert bot._parse_callback("discard:7") == ("cancel", 7, None)
    assert bot._parse_callback("unit:7:km") == ("unit", 7, "km")
    with pytest.raises(ValueError):
        bot._parse_callback("nonsense")
