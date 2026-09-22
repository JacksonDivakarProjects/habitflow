import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import bot
from tests.conftest import CHAT, edited, make_context, make_update, popup, replies

PREVIEW = {
    "audit_id": 7,
    "preview": "4 miles of Running today",
    "metric_source": "explicit",
    "draft_sql": "INSERT INTO daily_logs ...",
    "offline": False,
}

SUMMARY = {
    "log_id": 3, "habit": "Running", "amount": 4.0, "metric": "miles",
    "log_date": "2026-09-23", "when": "today", "streak_days": 3, "week_total": 12.0,
}


def run(coro):
    return asyncio.run(coro)


def _response(status, detail=None):
    return httpx.Response(status, json={"detail": detail} if detail is not None else {})


# ------------------------------------------------------------------
# Pure helpers
# ------------------------------------------------------------------
def test_escape_md():
    assert bot._escape_md("learning_sql *x* [a](b) `c`") == (
        "learning\\_sql \\*x\\* \\[a\\](b) \\`c\\`"
    )
    assert bot._escape_md("") == ""


def test_format_card_marks_suggested_unit_and_shows_sql():
    body = bot._format_card(7, "4 miles of Running", "suggested", "INSERT ...\n")

    assert "_(suggested unit)_" in body
    assert "_Draft #7_" in body
    assert body.endswith("```sql\nINSERT ...\n```")
    assert "offline" not in body


def test_format_card_without_sql():
    body = bot._format_card(7, "4 miles", "explicit", None)

    assert "```" not in body
    assert "suggested" not in body


def test_format_card_flags_offline_drafts():
    assert "AI is offline" in bot._format_card(7, "4 miles", "explicit", None, offline=True)


def test_card_has_approve_edit_discard():
    buttons = bot._card_markup(7).inline_keyboard[0]

    assert [b.text for b in buttons] == ["✅ Approve", "✏️ Edit", "🗑️ Discard"]
    assert [b.callback_data for b in buttons] == ["approve:7", "feedback:7", "discard:7"]


@pytest.mark.parametrize(
    "streak, expected_second_line",
    [
        (3, "🔥 3-day streak · 12 miles this week"),
        (1, "🌱 Day 1 of a new streak · 12 miles this week"),
        (0, "12 miles this week"),
    ],
)
def test_format_logged(streak, expected_second_line):
    text = bot._format_logged({**SUMMARY, "streak_days": streak})

    assert text.splitlines() == ["✅ Logged 4 miles of Running today", expected_second_line]


@pytest.mark.parametrize(
    "status, detail, expected",
    [
        (404, "Audit not found", "I can't find that anymore"),
        (409, "Audit status is superseded", "replaced by a newer one"),
        (409, "Audit status is executed", "already logged"),
        (409, "Audit status is cancelled", "discarded"),
        (409, "Audit status is failed", "no longer active"),
        (422, "Loop 1 failed after 3 attempts. Last: draft_sql failed: ...", "couldn't turn that"),
        (500, None, "went wrong on my side"),
        (502, "LLM call failed", "went wrong on my side"),
    ],
)
def test_friendly_error(status, detail, expected):
    text = bot._friendly_error(_response(status, detail))

    assert expected in text
    # Internals never leak to the chat.
    assert "Audit" not in text and "Loop" not in text and "{" not in text


def test_friendly_error_non_json_body():
    assert "went wrong" in bot._friendly_error(httpx.Response(500, text="<html>oops"))


# ------------------------------------------------------------------
# Authorization
# ------------------------------------------------------------------
def test_strangers_are_ignored(api):
    update = make_update("ran 4 miles", user_id=999)

    run(bot.handle_text(update, make_context()))

    assert api.requests == []
    update.message.reply_text.assert_not_awaited()


def test_strangers_cannot_press_buttons(api):
    update = make_update(user_id=999, callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert api.requests == []
    update.callback_query.edit_message_text.assert_not_awaited()


# ------------------------------------------------------------------
# Text messages
# ------------------------------------------------------------------
def test_new_text_is_drafted_and_card_sent(api):
    api.on("POST", "/internal/draft", json=PREVIEW)
    update, context = make_update("ran 4 miles", message_id=55), make_context()

    run(bot.handle_text(update, context))

    assert api.last_json() == {"chat_id": CHAT, "text": "ran 4 miles", "message_id": 55}
    call = update.message.reply_text.await_args
    assert "4 miles of Running today" in call.args[0]
    assert call.kwargs["parse_mode"] == "Markdown"
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "approve:7"


def test_typing_indicator_while_drafting(api):
    api.on("POST", "/internal/draft", json=PREVIEW)
    context = make_context()

    run(bot.handle_text(make_update("ran 4"), context))

    kwargs = context.bot.send_chat_action.await_args.kwargs
    assert kwargs == {"chat_id": CHAT, "action": "typing"}


def test_typing_failure_does_not_break_drafting(api):
    api.on("POST", "/internal/draft", json=PREVIEW)
    context = make_context()
    context.bot.send_chat_action.side_effect = RuntimeError("telegram hiccup")
    update = make_update("ran 4")

    run(bot.handle_text(update, context))

    assert "4 miles of Running" in replies(update)[0]


def test_offline_draft_card_says_so(api):
    api.on("POST", "/internal/draft", json={**PREVIEW, "offline": True})
    update = make_update("ran 4 miles")

    run(bot.handle_text(update, make_context()))

    assert "AI is offline" in replies(update)[0]


def test_draft_needing_unit_waits_for_clarification(api):
    api.on("POST", "/internal/draft", json={
        "audit_id": 7, "needs_input": "unit", "prompt": "What unit?",
    })
    update, context = make_update("ran 4"), make_context()

    run(bot.handle_text(update, context))

    assert replies(update) == ["What unit?"]
    assert context.user_data["awaiting_clarification"] == {"audit_id": 7}


def test_draft_proposing_habit_sends_create_card(api):
    api.on("POST", "/internal/draft", json={
        "audit_id": 7, "needs_input": "habit", "prompt": "Create pushups?",
    })
    update = make_update("20 pushups")

    run(bot.handle_text(update, make_context()))

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "create_habit:7", "cancel_habit:7",
    ]


def test_draft_422_gives_examples_not_internals(api):
    api.on("POST", "/internal/draft", status=422, json={
        "detail": "Loop 1 failed after 3 attempts. Last: draft_sql failed: WITH is not allowed",
    })
    update = make_update("blah")

    run(bot.handle_text(update, make_context()))

    assert replies(update) == [bot.COULDNT_UNDERSTAND]


def test_api_unreachable(api):
    api.on("POST", "/internal/draft", raises=httpx.ConnectError("refused"))
    update = make_update("ran 4")

    run(bot.handle_text(update, make_context()))

    assert replies(update) == [bot.UNREACHABLE]


def test_clarification_reply_goes_to_clarify(api):
    api.on("POST", "/internal/clarify", json=PREVIEW)
    update = make_update("km")
    context = make_context(awaiting_clarification={"audit_id": 7})

    run(bot.handle_text(update, context))

    assert api.last_json() == {"audit_id": 7, "value": "km"}
    assert "awaiting_clarification" not in context.user_data
    assert "4 miles of Running" in replies(update)[0]


def test_clarification_still_missing_unit_keeps_waiting(api):
    api.on("POST", "/internal/clarify", json={
        "audit_id": 7, "needs_input": "unit", "prompt": "Sorry, I didn't catch a unit",
    })
    context = make_context(awaiting_clarification={"audit_id": 7})
    update = make_update("the long way")

    run(bot.handle_text(update, context))

    assert context.user_data["awaiting_clarification"] == {"audit_id": 7}
    assert replies(update) == ["Sorry, I didn't catch a unit"]


@pytest.mark.parametrize("status", [404, 409])
def test_expired_clarification_is_cleared(api, status):
    """Bug 6: a dead clarification used to swallow every later message."""
    api.on("POST", "/internal/clarify", status=status, json={"detail": "gone"})
    update = make_update("km")
    context = make_context(awaiting_clarification={"audit_id": 7})

    run(bot.handle_text(update, context))

    assert "awaiting_clarification" not in context.user_data
    assert "expired" in replies(update)[0]


def test_clarify_server_error_keeps_waiting(api):
    api.on("POST", "/internal/clarify", status=502, json={"detail": "LLM down"})
    context = make_context(awaiting_clarification={"audit_id": 7})
    update = make_update("km")

    run(bot.handle_text(update, context))

    assert context.user_data["awaiting_clarification"] == {"audit_id": 7}
    assert "/cancel" in replies(update)[0]


def test_feedback_reply_edits_original_card(api):
    api.on("POST", "/internal/feedback", json={**PREVIEW, "preview": "6 miles of Running today"})
    update = make_update("no, 6 miles")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert api.last_json() == {"audit_id": 7, "feedback": "no, 6 miles"}
    edit = context.bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 99
    assert edit["text"].startswith("🔄 Updated")
    assert "6 miles of Running" in edit["text"]
    assert "awaiting_feedback_for" not in context.user_data


def test_feedback_that_cannot_be_applied_stays_open(api):
    api.on("POST", "/internal/feedback", status=422, json={"detail": "Loop 2 failed"})
    update = make_update("asdf")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert context.user_data == {"awaiting_feedback_for": 7, "feedback_card_message_id": 99}
    assert "couldn't apply that change" in replies(update)[0]


def test_feedback_on_superseded_draft_closes(api):
    api.on("POST", "/internal/feedback", status=409, json={"detail": "Audit status is superseded"})
    update = make_update("6 miles")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert context.user_data == {}
    assert replies(update) == ["This draft was replaced by a newer one."]


@pytest.mark.parametrize(
    "path, user_data, text",
    [
        ("/internal/draft", {}, "ran 4"),
        ("/internal/clarify", {"awaiting_clarification": {"audit_id": 7}}, "km"),
        ("/internal/feedback", {"awaiting_feedback_for": 7}, "6"),
    ],
)
def test_llm_backed_calls_outlast_api_retries(api, path, user_data, text):
    """Bug 5: the bot gave up before the API's 3 LLM attempts could finish."""
    api.on("POST", path, json=PREVIEW)

    run(bot.handle_text(make_update(text), make_context(**user_data)))

    assert api.timeouts[path] == bot.settings.api_llm_timeout_seconds
    assert bot.settings.api_llm_timeout_seconds > 3 * 20


# ------------------------------------------------------------------
# Buttons
# ------------------------------------------------------------------
def test_approve_shows_progress_and_undo_button(api):
    api.on("POST", "/internal/execute", json={
        "status": "executed", "log_id": 3, "summary": SUMMARY,
    })
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7}
    call = update.callback_query.edit_message_text.await_args
    assert call.args[0] == (
        "✅ Logged 4 miles of Running today\n🔥 3-day streak · 12 miles this week"
    )
    undo = call.kwargs["reply_markup"].inline_keyboard[0][0]
    assert (undo.text, undo.callback_data) == ("↩️ Undo", "undo:3")


def test_approve_twice_says_already_logged(api):
    api.on("POST", "/internal/execute", json={"status": "already_executed", "log_id": 3})
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert edited(update) == "✅ Already logged."


def test_approve_superseded_card_explains_in_popup(api):
    api.on("POST", "/internal/execute", status=409, json={"detail": "Audit status is superseded"})
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert popup(update) == "This draft was replaced by a newer one."
    update.callback_query.edit_message_text.assert_not_awaited()  # card left intact


def test_button_when_api_unreachable(api):
    api.on("POST", "/internal/execute", raises=httpx.ConnectError("refused"))
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert popup(update) == bot.UNREACHABLE


def test_every_button_answers_exactly_once(api):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    update = make_update(callback_data="discard:7")

    run(bot.button_callback(update, make_context()))

    assert update.callback_query.answer.await_count == 1


def test_approving_closes_open_edit_for_that_card(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "summary": SUMMARY})
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.button_callback(make_update(callback_data="approve:7"), context))

    assert context.user_data == {}


def test_approving_other_card_keeps_open_edit(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "summary": SUMMARY})
    context = make_context(awaiting_feedback_for=8, feedback_card_message_id=99)

    run(bot.button_callback(make_update(callback_data="approve:7"), context))

    assert context.user_data["awaiting_feedback_for"] == 8


def test_discard_button(api):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    update = make_update(callback_data="discard:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7}
    assert edited(update) == "🗑️ Discarded."


def test_undo_button(api):
    api.on("POST", "/internal/undo", json={
        "status": "undone", "log_id": 3, "preview": "4 miles of Running today",
    })
    update = make_update(callback_data="undo:3")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"log_id": 3}
    assert edited(update) == "↩️ Undone: 4 miles of Running today."


def test_undo_button_twice(api):
    api.on("POST", "/internal/undo", json={
        "status": "already_undone", "log_id": 3, "preview": "4 miles of Running today",
    })
    update = make_update(callback_data="undo:3")

    run(bot.button_callback(update, make_context()))

    assert edited(update).startswith("↩️ Already undone")


def test_edit_button_keeps_card_and_asks_for_fix(api):
    update = make_update(callback_data="feedback:7", message_id=99)
    context = make_context()

    run(bot.button_callback(update, context))

    assert context.user_data == {"awaiting_feedback_for": 7, "feedback_card_message_id": 99}
    assert api.requests == []
    call = update.callback_query.edit_message_text.await_args
    assert call.args[0].startswith("📝 4 miles of Running today")
    assert "What should change?" in call.args[0]
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "approve:7"


def test_create_habit_needing_unit(api):
    api.on("POST", "/internal/approve_habit", json={
        "audit_id": 7, "needs_input": "unit", "prompt": "What unit?",
    })
    update, context = make_update(callback_data="create_habit:7"), make_context()

    run(bot.button_callback(update, context))

    assert api.last_json() == {"audit_id": 7, "accept": True}
    assert context.user_data["awaiting_clarification"] == {"audit_id": 7}


def test_create_habit_shows_card(api):
    api.on("POST", "/internal/approve_habit", json={**PREVIEW, "preview": "20 reps of Pushups today"})
    update = make_update(callback_data="create_habit:7")

    run(bot.button_callback(update, make_context()))

    assert edited(update).startswith("✨ New habit created.")
    assert "20 reps of Pushups" in edited(update)


def test_cancel_habit(api):
    api.on("POST", "/internal/approve_habit", json={"audit_id": 7, "preview": "Cancelled."})
    update = make_update(callback_data="cancel_habit:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7, "accept": False}
    assert edited(update) == "OK, I didn't create it."


@pytest.mark.parametrize("data", ["approve:not-a-number", "explode:7", "nocolon"])
def test_malformed_button(api, data):
    update = make_update(callback_data=data)

    run(bot.button_callback(update, make_context()))

    assert api.requests == []
    assert edited(update) == "Malformed button."


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
def test_cancel_clears_all_waiting_state():
    update = make_update("/cancel")
    context = make_context(
        awaiting_clarification={"audit_id": 1},
        awaiting_feedback_for=2,
        feedback_card_message_id=3,
    )

    run(bot.cancel_cmd(update, context))

    assert context.user_data == {}
    assert replies(update) == ["OK, cancelled."]


def test_cancel_with_nothing_open():
    update = make_update("/cancel")

    run(bot.cancel_cmd(update, make_context()))

    assert replies(update) == ["There's nothing to cancel."]


def test_undo_command(api):
    api.on("POST", "/internal/undo", json={
        "status": "undone", "log_id": 3, "preview": "4 miles of Running today",
    })
    update = make_update("/undo")

    run(bot.undo_cmd(update, make_context()))

    assert api.last_json() == {}
    assert replies(update) == ["↩️ Undid your last log: 4 miles of Running today."]


def test_undo_command_nothing_to_undo(api):
    api.on("POST", "/internal/undo", status=404, json={"detail": "Nothing to undo"})
    update = make_update("/undo")

    run(bot.undo_cmd(update, make_context()))

    assert replies(update) == ["Nothing to undo."]


def test_today_command_totals_per_habit_and_unit(api):
    api.on("GET", "/internal/today", json={"date": "2026-09-23", "logs": [
        {"log_id": 1, "habit": "Running", "amount": 2.0, "metric": "miles"},
        {"log_id": 2, "habit": "Reading", "amount": 20.0, "metric": "pages"},
        {"log_id": 3, "habit": "Running", "amount": 1.5, "metric": "miles"},
    ]})
    update = make_update("/today")

    run(bot.today_cmd(update, make_context()))

    assert replies(update) == [
        "📅 Today so far:\n• Running: 3.5 miles\n• Reading: 20 pages"
    ]


def test_today_command_empty(api):
    api.on("GET", "/internal/today", json={"date": "2026-09-23", "logs": []})
    update = make_update("/today")

    run(bot.today_cmd(update, make_context()))

    assert replies(update)[0].startswith("Nothing logged yet today.")


def test_habits_command(api):
    api.on("GET", "/internal/habits", json=[
        {"display_name": "Running", "metric": "miles"},
        {"display_name": "Pushups", "metric": None},
    ])
    update = make_update("/habits")

    run(bot.habits_cmd(update, make_context()))

    assert replies(update) == [
        "Tracked habits:\n• Running (miles)\n• Pushups (no default unit)"
    ]


def test_stats_command(api):
    api.on("GET", "/internal/stats", json=[
        {"habit": "Running", "metric": "miles", "total": 6.5, "days": 2, "streak_days": 2},
        {"habit": "Reading", "metric": "pages", "total": 20, "days": 1, "streak_days": 1},
    ])
    update = make_update("/stats")

    run(bot.stats_cmd(update, make_context()))

    assert replies(update) == [
        "📊 Last 30 days:\n"
        "• Running: 6.5 miles on 2 days · 🔥 2\n"
        "• Reading: 20 pages on 1 day"
    ]


def test_stats_command_empty(api):
    api.on("GET", "/internal/stats", json=[])
    update = make_update("/stats")

    run(bot.stats_cmd(update, make_context()))

    assert replies(update)[0].startswith("No logs in the last 30 days.")


def test_commands_unreachable_api(api):
    api.on("GET", "/internal/stats", raises=httpx.ConnectError("refused"))
    update = make_update("/stats")

    run(bot.stats_cmd(update, make_context()))

    assert replies(update) == [bot.UNREACHABLE]


def test_help_lists_every_command():
    update = make_update("/help")

    run(bot.help_cmd(update, make_context()))

    for name, _ in bot.COMMANDS:
        assert f"/{name}" in replies(update)[0]


def test_startup_registers_command_menu(api):
    api.on("GET", "/health", json={"status": "ok"})
    api.on("GET", "/internal/reminders", json=[])
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()), job_queue=None)

    run(bot.on_startup(app))

    commands = app.bot.set_my_commands.await_args.args[0]
    assert [c.command for c in commands] == [name for name, _ in bot.COMMANDS]
