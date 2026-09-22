import asyncio

import pytest

import bot
from tests.conftest import CHAT, make_context, make_update, replies

PREVIEW = {
    "audit_id": 7,
    "preview": "4 miles of Running on 2026-09-23",
    "metric_source": "explicit",
    "draft_sql": "INSERT INTO daily_logs ...",
}


def run(coro):
    return asyncio.run(coro)


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

    assert "_(suggested)_" in body
    assert "_Audit #7_" in body
    assert body.endswith("```sql\nINSERT ...\n```")


def test_format_card_without_sql():
    body = bot._format_card(7, "4 miles", "explicit", None)

    assert "```" not in body
    assert "suggested" not in body


def test_card_buttons_carry_audit_id():
    buttons = bot._card_markup(7).inline_keyboard[0]

    assert [b.callback_data for b in buttons] == ["approve:7", "feedback:7"]


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
    update = make_update("ran 4 miles", message_id=55)

    run(bot.handle_text(update, make_context()))

    assert api.last_json() == {"chat_id": CHAT, "text": "ran 4 miles", "message_id": 55}
    call = update.message.reply_text.await_args
    assert "4 miles of Running" in call.args[0]
    assert call.kwargs["parse_mode"] == "Markdown"
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "approve:7"


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


def test_draft_422_shows_detail(api):
    api.on("POST", "/internal/draft", status=422, json={"detail": "Couldn't parse"})
    update = make_update("blah")

    run(bot.handle_text(update, make_context()))

    assert replies(update) == ["Couldn't parse"]


def test_clarification_reply_goes_to_clarify(api):
    api.on("POST", "/internal/clarify", json=PREVIEW)
    update = make_update("km")
    context = make_context(awaiting_clarification={"audit_id": 7})

    run(bot.handle_text(update, context))

    assert api.last_json() == {"audit_id": 7, "value": "km"}
    assert "awaiting_clarification" not in context.user_data
    assert "4 miles of Running" in replies(update)[0]


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
    api.on("POST", "/internal/feedback", json={**PREVIEW, "preview": "6 miles of Running"})
    update = make_update("no, 6 miles")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert api.last_json() == {"audit_id": 7, "feedback": "no, 6 miles"}
    edit = context.bot.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 99
    assert "6 miles of Running" in edit["text"]
    assert "awaiting_feedback_for" not in context.user_data


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


def test_cancel_clears_all_waiting_state():
    update = make_update("/cancel")
    context = make_context(
        awaiting_clarification={"audit_id": 1},
        awaiting_feedback_for=2,
        feedback_card_message_id=3,
    )

    run(bot.cancel_cmd(update, context))

    assert context.user_data == {}


# ------------------------------------------------------------------
# Buttons
# ------------------------------------------------------------------
def test_approve_executes(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3})
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7}
    assert "Logged. Audit #7 (log 3)" in update.callback_query.edit_message_text.await_args.args[0]


def test_approve_twice_says_already_logged(api):
    api.on("POST", "/internal/execute", json={"status": "already_executed", "log_id": None})
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert "Already logged" in update.callback_query.edit_message_text.await_args.args[0]


def test_approve_superseded_card_shows_error(api):
    api.on("POST", "/internal/execute", status=409, json={"detail": "superseded"})
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    assert "409" in update.callback_query.edit_message_text.await_args.args[0]


def test_feedback_button_waits_for_next_message(api):
    update = make_update(callback_data="feedback:7", message_id=99)
    context = make_context()

    run(bot.button_callback(update, context))

    assert context.user_data == {"awaiting_feedback_for": 7, "feedback_card_message_id": 99}
    assert api.requests == []


def test_create_habit_needing_unit(api):
    api.on("POST", "/internal/approve_habit", json={
        "audit_id": 7, "needs_input": "unit", "prompt": "What unit?",
    })
    update, context = make_update(callback_data="create_habit:7"), make_context()

    run(bot.button_callback(update, context))

    assert api.last_json() == {"audit_id": 7, "accept": True}
    assert context.user_data["awaiting_clarification"] == {"audit_id": 7}


def test_cancel_habit(api):
    api.on("POST", "/internal/approve_habit", json={"audit_id": 7, "preview": "Cancelled."})
    update = make_update(callback_data="cancel_habit:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7, "accept": False}
    assert "Cancelled" in update.callback_query.edit_message_text.await_args.args[0]


def test_malformed_button(api):
    update = make_update(callback_data="approve:not-a-number")

    run(bot.button_callback(update, make_context()))

    assert api.requests == []
    assert update.callback_query.edit_message_text.await_args.args[0] == "Malformed button."


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
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
        {"habit": "Running", "metric": "miles", "total": 6.5, "days": 2},
    ])
    update = make_update("/stats")

    run(bot.stats_cmd(update, make_context()))

    assert replies(update) == ["Last 30 days:\n• Running: 6.5 miles over 2 day(s)"]


def test_stats_command_empty(api):
    api.on("GET", "/internal/stats", json=[])
    update = make_update("/stats")

    run(bot.stats_cmd(update, make_context()))

    assert replies(update) == ["No logs in the last 30 days."]
