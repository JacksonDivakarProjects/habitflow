"""Handlers end to end against a fake API: messages, the two conversations, buttons, commands."""

import asyncio
import json

import httpx
import pytest

import bot
from tests.conftest import (
    CHAT,
    buttons,
    edited,
    make_context,
    make_update,
    markup_of,
    popup,
    query_replies,
    replies,
)

ITEM = {"habit": "Running", "amount": 4.0, "unit": "miles", "quantity": "4 miles",
        "log_date": "2026-09-24", "when": "today"}
CARD = {"audit_id": 7, "status": "pending", "preview": "4 miles of Running today",
        "items": [ITEM], "metric_source": "explicit", "draft_sql": "INSERT INTO daily_logs ...",
        "offline": False, "skipped": [], "needs_input": None, "unit_options": []}
UNIT_Q = {"audit_id": 7, "needs_input": "unit", "prompt": "Got it: 4 of Running today. What unit?",
          "unit_options": ["miles", "km", "meters"], "items": []}
HABIT_Q = {"audit_id": 8, "needs_input": "habit", "prompt": "“Pushups” is a new habit. Create it?"}
SUMMARY = {"log_id": 3, "habit": "Running", "amount": 4.0, "metric": "miles",
           "log_date": "2026-09-24", "when": "today", "streak_days": 3, "week_total": 12.0}
ANSWER = {"query_id": 9, "ok": True, "answer": "60 pages of Reading this month.",
          "source": "template", "sql": "SELECT ...", "columns": ["unit", "total"],
          "rows": [["pages", 60]], "row_count": 1, "truncated": False}


def run(coro):
    return asyncio.run(coro)


def message(kind, **fields):
    return {"kind": kind, "classified_by": "rules", "draft": None, "answer": None,
            "text": None, **fields}


def body(api, index=-1) -> dict:
    return json.loads(api.requests[index].content)


def paths(api) -> list[str]:
    return [f"{r.method} {r.url.path}" for r in api.requests]


def parse_mode(mock):
    return mock.await_args.kwargs.get("parse_mode")


# ------------------------------------------------------------------
# Who may talk to the bot
# ------------------------------------------------------------------
def test_strangers_are_ignored(api):
    update = make_update("ran 4 miles", user_id=999)
    run(bot.handle_text(update, make_context()))
    assert api.requests == [] and replies(update) == []


def test_strangers_cannot_press_buttons(api):
    update = make_update(callback_data="save:7", user_id=999)
    run(bot.button_callback(update, make_context()))
    assert api.requests == []
    update.callback_query.answer.assert_awaited_once()


# ------------------------------------------------------------------
# A new message
# ------------------------------------------------------------------
def test_a_log_gets_a_card(api):
    api.on("POST", "/internal/message", json=message("log", draft=CARD))
    update, context = make_update("ran 4 miles", message_id=55), make_context()
    run(bot.handle_text(update, context))

    assert body(api) == {"chat_id": CHAT, "text": "ran 4 miles", "message_id": 55}
    assert api.timeouts["/internal/message"] == bot.settings.api_llm_timeout_seconds
    assert replies(update) == ["📝 <b>Log this?</b>\n• <b>Running</b> · 4 miles · today"]
    assert parse_mode(update.message.reply_text) == "HTML"
    assert ("✅ Save", "save:7") in buttons(markup_of(update.message.reply_text))
    context.bot.send_chat_action.assert_awaited()


def test_typing_failure_does_not_break_anything(api):
    api.on("POST", "/internal/message", json=message("log", draft=CARD))
    update, context = make_update("ran 4 miles"), make_context()
    context.bot.send_chat_action.side_effect = RuntimeError("telegram hiccup")
    run(bot.handle_text(update, context))
    assert replies(update)[0].startswith("📝")


def test_html_rejected_by_telegram_falls_back_to_plain_text(api):
    api.on("POST", "/internal/message", json=message("log", draft=CARD))
    update = make_update("ran 4 miles")
    first = update.message.reply_text.side_effect
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Bad Request: can't parse entities")
        return first(*args, **kwargs)

    update.message.reply_text.side_effect = flaky
    run(bot.handle_text(update, make_context()))
    assert replies(update)[1] == "📝 Log this?\n• Running · 4 miles · today"


def test_a_log_needing_a_unit_asks_with_buttons(api):
    api.on("POST", "/internal/message", json=message("log", draft=UNIT_Q))
    update, context = make_update("ran 4"), make_context()
    run(bot.handle_text(update, context))
    assert replies(update)[0].startswith("❓ Got it: 4 of Running today. What unit?")
    assert ("km", "unit:7:km") in buttons(markup_of(update.message.reply_text))
    assert context.user_data[bot.UNIT]["audit_id"] == 7
    assert context.user_data[bot.UNIT]["message_id"]  # the question message, to tidy up later


def test_a_new_habit_is_offered(api):
    api.on("POST", "/internal/message", json=message("log", draft=HABIT_Q))
    update = make_update("did 20 pushups")
    run(bot.handle_text(update, make_context()))
    assert replies(update) == ["✨ “Pushups” is a new habit. Create it?"]
    assert buttons(markup_of(update.message.reply_text))[0] == ("✨ Create habit", "create:8")


def test_a_question_gets_an_answer(api):
    api.on("POST", "/internal/message", json=message("answer", answer=ANSWER))
    update = make_update("how much did I read this month?")
    run(bot.handle_text(update, make_context()))
    assert replies(update) == ["💬 60 pages of Reading this month."]
    assert buttons(markup_of(update.message.reply_text)) == [("🔍 SQL", "qsql:9")]


def test_an_answer_with_rows_shows_a_table(api):
    answer = {**ANSWER, "source": "llm", "answer": "Most on Wednesdays.",
              "columns": ["weekday_name", "pages"], "rows": [["Tuesday", 20], ["Wednesday", 30]]}
    api.on("POST", "/internal/message", json=message("answer", answer=answer))
    update = make_update("what's my reading pattern")
    run(bot.handle_text(update, make_context()))
    assert "📊 <i>pages by weekday name</i>" in replies(update)[0]


def test_small_talk_gets_help(api):
    api.on("POST", "/internal/message", json=message("chat", text="help"))
    update = make_update("hi")
    run(bot.handle_text(update, make_context()))
    assert replies(update) == [bot.HELP_TEXT]


def test_unusable_log_gets_the_apis_explanation(api):
    api.on("POST", "/internal/message", json=message("error", text="I couldn't turn that into a log."))
    update = make_update("7 zzz")
    run(bot.handle_text(update, make_context()))
    assert replies(update) == ["I couldn't turn that into a log."]


def test_api_unreachable(api):
    api.on("POST", "/internal/message", raises=httpx.ConnectError("down"))
    update = make_update("ran 4 miles")
    run(bot.handle_text(update, make_context()))
    assert replies(update) == [bot.UNREACHABLE]


def test_api_error_is_friendly(api):
    api.on("POST", "/internal/message", status=500, json={"detail": "Traceback ..."})
    update = make_update("ran 4 miles")
    run(bot.handle_text(update, make_context()))
    assert "went wrong on my side" in replies(update)[0]
    assert "Traceback" not in replies(update)[0]


# ------------------------------------------------------------------
# Unit question
# ------------------------------------------------------------------
def unit_context():
    return make_context(**{bot.UNIT: {"audit_id": 7, "message_id": 70}})


def test_typed_unit(api):
    api.on("POST", "/internal/clarify", json=CARD)
    update, context = make_update("km"), unit_context()
    run(bot.handle_text(update, context))
    assert body(api) == {"audit_id": 7, "value": "km"}
    assert replies(update)[0].startswith("📝 <b>Log this?</b>")
    assert bot.UNIT not in context.user_data
    context.bot.edit_message_reply_markup.assert_awaited_with(
        chat_id=CHAT, message_id=70, reply_markup=None)  # the old question's buttons go


def test_typed_answer_without_a_unit_asks_again(api):
    api.on("POST", "/internal/clarify", json={**UNIT_Q, "prompt": "Sorry, I didn't catch a unit."})
    update, context = make_update("hmm"), unit_context()
    run(bot.handle_text(update, context))
    assert replies(update)[0].startswith("❓ Sorry, I didn't catch a unit.")
    assert context.user_data[bot.UNIT]["audit_id"] == 7


def test_unit_button(api):
    api.on("POST", "/internal/clarify", json=CARD)
    update, context = make_update(callback_data="unit:7:km", message_id=70), unit_context()
    run(bot.button_callback(update, context))
    assert body(api) == {"audit_id": 7, "value": "km"}
    assert edited(update).startswith("📝 <b>Log this?</b>")   # the question becomes the card
    assert bot.UNIT not in context.user_data


def test_a_new_log_typed_instead_of_a_unit_is_logged(api):
    api.on("POST", "/internal/clarify", status=409,
           json={"detail": {"code": "new_log", "dropped": "ran 4"}})
    api.on("POST", "/internal/message", json=message("log", draft=CARD))
    update, context = make_update("read 20 pages"), unit_context()
    run(bot.handle_text(update, context))
    assert "dropped “ran 4”" in replies(update)[0]
    assert replies(update)[1].startswith("📝")
    assert paths(api) == ["POST /internal/clarify", "POST /internal/message"]
    assert bot.UNIT not in context.user_data


def test_expired_unit_question_is_cleared(api):
    api.on("POST", "/internal/clarify", status=409, json={"detail": "Audit status is superseded"})
    update, context = make_update("km"), unit_context()
    run(bot.handle_text(update, context))
    assert "expired" in replies(update)[0] and bot.UNIT not in context.user_data


def test_unit_server_error_keeps_waiting(api):
    api.on("POST", "/internal/clarify", status=500, json={"detail": "x"})
    update, context = make_update("km"), unit_context()
    run(bot.handle_text(update, context))
    assert "/cancel" in replies(update)[0] and bot.UNIT in context.user_data


def test_unit_unreachable_keeps_waiting(api):
    api.on("POST", "/internal/clarify", raises=httpx.ConnectError("down"))
    update, context = make_update("km"), unit_context()
    run(bot.handle_text(update, context))
    assert bot.UNREACHABLE in replies(update)[0] and bot.UNIT in context.user_data


@pytest.mark.parametrize("word", ["cancel", "Never mind", "nvm!", "stop."])
def test_cancel_word_drops_the_unit_question(api, word):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    update, context = make_update(word), unit_context()
    run(bot.handle_text(update, context))
    assert body(api) == {"audit_id": 7}
    assert replies(update) == ["OK, dropped it."] and bot.UNIT not in context.user_data


def test_a_question_during_a_unit_question_is_answered_and_the_question_kept(api):
    api.on("POST", "/internal/message", json=message("answer", answer=ANSWER))
    update, context = make_update("how much did I read this month?"), unit_context()
    run(bot.handle_text(update, context))
    assert paths(api) == ["POST /internal/message"]
    assert replies(update)[0].startswith("💬")
    assert "still waiting for the unit" in replies(update)[1]
    assert context.user_data[bot.UNIT]["audit_id"] == 7


# ------------------------------------------------------------------
# Changing a draft
# ------------------------------------------------------------------
def edit_context(audit_id=7):
    return make_context(**{bot.EDIT: {"audit_id": audit_id, "card_message_id": 10,
                                      "prompt_message_id": 11}})


def test_change_button_asks_what_to_change(api):
    update, context = make_update(callback_data="change:7", message_id=10), make_context()
    run(bot.button_callback(update, context))
    assert api.requests == []
    prompt = query_replies(update)[0]
    assert "What should change?" in prompt and "• Running · 4 miles · today" in prompt
    assert ("📅 It was yesterday", "fix:7:yesterday") in buttons(
        markup_of(update.callback_query.message.reply_text))
    state = context.user_data[bot.EDIT]
    assert (state["audit_id"], state["card_message_id"]) == (7, 10) and state["prompt_message_id"]
    update.callback_query.answer.assert_awaited_once()


def test_typed_change_retires_the_old_card_and_sends_the_new_one(api):
    api.on("POST", "/internal/feedback", json={**CARD, "items": [{**ITEM, "quantity": "6 miles"}]})
    update, context = make_update("6 miles, not 4"), edit_context()
    run(bot.handle_text(update, context))
    assert body(api) == {"audit_id": 7, "feedback": "6 miles, not 4"}
    assert api.timeouts["/internal/feedback"] == bot.settings.api_llm_timeout_seconds
    retired = context.bot.edit_message_text.await_args.kwargs
    assert retired["message_id"] == 10 and "Changed" in retired["text"]
    assert replies(update) == ["🔄 <b>Updated. Log this?</b>\n• <b>Running</b> · 6 miles · today"]
    assert bot.EDIT not in context.user_data
    context.bot.edit_message_reply_markup.assert_awaited_with(
        chat_id=CHAT, message_id=11, reply_markup=None)


def test_change_that_cannot_be_applied_stays_open(api):
    api.on("POST", "/internal/feedback", status=422, json={"detail": "no"})
    update, context = make_update("make it purple"), edit_context()
    run(bot.handle_text(update, context))
    assert "couldn't apply that change" in replies(update)[0]
    assert bot.EDIT in context.user_data


def test_change_survives_a_network_error(api):
    api.on("POST", "/internal/feedback", raises=httpx.ConnectError("down"))
    update, context = make_update("6 miles"), edit_context()
    run(bot.handle_text(update, context))
    assert bot.UNREACHABLE in replies(update)[0] and bot.EDIT in context.user_data


def test_change_on_a_superseded_draft_closes(api):
    api.on("POST", "/internal/feedback", status=409, json={"detail": "Audit status is superseded"})
    update, context = make_update("6 miles"), edit_context()
    run(bot.handle_text(update, context))
    assert "replaced by a newer one" in replies(update)[0] and bot.EDIT not in context.user_data


def test_change_that_needs_a_unit_asks_for_it(api):
    api.on("POST", "/internal/feedback", json=UNIT_Q)
    update, context = make_update("make it pushups"), edit_context()
    run(bot.handle_text(update, context))
    assert replies(update)[0].startswith("❓")
    assert context.user_data[bot.UNIT]["audit_id"] == 7 and bot.EDIT not in context.user_data


def test_cancel_word_keeps_the_draft(api):
    update, context = make_update("cancel"), edit_context()
    run(bot.handle_text(update, context))
    assert api.requests == [] and replies(update) == ["OK, I left the draft as it was."]
    assert bot.EDIT not in context.user_data


def test_sentence_with_cancel_in_it_is_still_a_change(api):
    api.on("POST", "/internal/feedback", json=CARD)
    update, context = make_update("cancel the km, it was miles"), edit_context()
    run(bot.handle_text(update, context))
    assert body(api)["feedback"] == "cancel the km, it was miles"


def test_a_question_during_a_change_is_answered_and_the_change_kept(api):
    api.on("POST", "/internal/message", json=message("answer", answer=ANSWER))
    update, context = make_update("how much did I read this month?"), edit_context()
    run(bot.handle_text(update, context))
    assert paths(api) == ["POST /internal/message"]
    assert "still waiting for your change" in replies(update)[1]
    assert bot.EDIT in context.user_data


@pytest.mark.parametrize("fix,feedback", [("yesterday", "it was yesterday"), ("today", "it was today")])
def test_quick_fix_buttons(api, fix, feedback):
    api.on("POST", "/internal/feedback", json=CARD)
    update, context = make_update(callback_data=f"fix:7:{fix}", message_id=11), edit_context()
    run(bot.button_callback(update, context))
    assert body(api) == {"audit_id": 7, "feedback": feedback}
    assert query_replies(update)[0].startswith("🔄 <b>Updated")
    update.callback_query.answer.assert_awaited_once()


def test_quick_fix_from_an_old_prompt_still_applies(api):
    api.on("POST", "/internal/feedback", json=CARD)
    update, context = make_update(callback_data="fix:7:yesterday", message_id=11), make_context()
    run(bot.button_callback(update, context))
    assert body(api)["audit_id"] == 7 and bot.EDIT not in context.user_data


def test_keep_as_is(api):
    update, context = make_update(callback_data="keep:7", message_id=11), edit_context()
    run(bot.button_callback(update, context))
    assert api.requests == [] and edited(update) == "OK, I left the draft as it was."
    assert bot.EDIT not in context.user_data


def test_changing_another_card_switches_target(api):
    update, context = make_update(callback_data="change:9", message_id=90), edit_context(7)
    run(bot.button_callback(update, context))
    assert context.user_data[bot.EDIT]["audit_id"] == 9
    context.bot.edit_message_reply_markup.assert_awaited_with(
        chat_id=CHAT, message_id=11, reply_markup=None)  # the old prompt is tidied


# ------------------------------------------------------------------
# Card buttons
# ------------------------------------------------------------------
def test_save_shows_progress_and_undo(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "log_ids": [3],
                                              "summary": SUMMARY, "summaries": [SUMMARY]})
    update = make_update(callback_data="save:7")
    run(bot.button_callback(update, make_context()))
    assert body(api) == {"audit_id": 7}
    assert edited(update).startswith("✅ <b>Saved</b>\n• <b>Running</b> · 4 miles · today")
    assert "🔥 3-day streak · 12 miles this week" in edited(update)
    assert buttons(markup_of(update.callback_query.edit_message_text)) == [("↩️ Undo", "undo:3")]


def test_save_several_undoes_all_together(api):
    other = {**SUMMARY, "log_id": 4, "habit": "Reading", "amount": 20, "metric": "pages"}
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "log_ids": [3, 4],
                                              "summary": SUMMARY, "summaries": [SUMMARY, other]})
    update = make_update(callback_data="save:7")
    run(bot.button_callback(update, make_context()))
    assert "• <b>Reading</b> · 20 pages · today" in edited(update)
    assert buttons(markup_of(update.callback_query.edit_message_text)) == [
        ("↩️ Undo", "undo_audit:7")]


def test_save_twice(api):
    api.on("POST", "/internal/execute", json={"status": "already_executed", "log_id": 3})
    update = make_update(callback_data="save:7")
    run(bot.button_callback(update, make_context()))
    assert edited(update) == "✅ Already saved."


def test_save_on_a_superseded_card_explains_in_a_popup(api):
    api.on("POST", "/internal/execute", status=409, json={"detail": "Audit status is superseded"})
    update = make_update(callback_data="save:7")
    run(bot.button_callback(update, make_context()))
    assert "replaced by a newer one" in popup(update)
    update.callback_query.edit_message_text.assert_not_awaited()  # card left alone


def test_button_when_api_unreachable(api):
    api.on("POST", "/internal/execute", raises=httpx.ConnectError("down"))
    update = make_update(callback_data="save:7")
    run(bot.button_callback(update, make_context()))
    assert popup(update) == bot.UNREACHABLE


def test_saving_closes_an_open_change_for_that_card(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "log_ids": [3],
                                              "summaries": [SUMMARY]})
    context = edit_context(7)
    run(bot.button_callback(make_update(callback_data="save:7"), context))
    assert bot.EDIT not in context.user_data


def test_saving_another_card_keeps_the_open_change(api):
    api.on("POST", "/internal/execute", json={"status": "executed", "log_id": 3, "log_ids": [3],
                                              "summaries": [SUMMARY]})
    context = edit_context(7)
    run(bot.button_callback(make_update(callback_data="save:9"), context))
    assert context.user_data[bot.EDIT]["audit_id"] == 7


def test_cancel_button(api):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    update, context = make_update(callback_data="cancel:7"), unit_context()
    run(bot.button_callback(update, context))
    assert edited(update) == "✖ Cancelled. Nothing was saved."
    assert bot.UNIT not in context.user_data   # the unit question for it is gone too


def test_undo_buttons(api):
    api.on("POST", "/internal/undo", json={"status": "undone", "preview": "4 miles of Running today"})
    update = make_update(callback_data="undo:3")
    run(bot.button_callback(update, make_context()))
    assert body(api) == {"log_id": 3}
    assert edited(update) == "↩️ <b>Undone</b>: 4 miles of Running today"

    api.on("POST", "/internal/undo", json={"status": "already_undone", "preview": "x"})
    update = make_update(callback_data="undo_audit:7")
    run(bot.button_callback(update, make_context()))
    assert body(api) == {"audit_id": 7}
    assert edited(update).startswith("↩️ <b>Already undone</b>")


def test_create_habit_then_card(api):
    api.on("POST", "/internal/approve_habit", json={**CARD, "audit_id": 8})
    update = make_update(callback_data="create:8")
    run(bot.button_callback(update, make_context()))
    assert body(api) == {"audit_id": 8, "accept": True}
    assert edited(update).startswith("✨ <b>New habit created. Log this?</b>")


def test_create_habit_that_needs_a_unit(api):
    api.on("POST", "/internal/approve_habit", json={**UNIT_Q, "audit_id": 8})
    update, context = make_update(callback_data="create:8", message_id=80), make_context()
    run(bot.button_callback(update, context))
    assert edited(update).startswith("❓")
    assert context.user_data[bot.UNIT] == {"audit_id": 8, "message_id": 80}


def test_decline_new_habit(api):
    api.on("POST", "/internal/approve_habit", json={"audit_id": 8, "preview": "Cancelled."})
    update = make_update(callback_data="nocreate:8")
    run(bot.button_callback(update, make_context()))
    assert body(api) == {"audit_id": 8, "accept": False}
    assert edited(update) == "OK, I didn't create it."


def test_draft_sql_button(api):
    api.on("GET", "/internal/drafts/7", json={"audit_id": 7, "status": "pending",
                                              "draft_sql": "INSERT INTO daily_logs <x>",
                                              "final_sql": None})
    update = make_update(callback_data="sql:7")
    run(bot.button_callback(update, make_context()))
    assert query_replies(update) == [
        "🔍 <b>SQL for this log</b>\n<pre>INSERT INTO daily_logs &lt;x&gt;</pre>"]
    assert buttons(markup_of(update.callback_query.message.reply_text)) == [("✖ Close", "close:0")]
    update.callback_query.edit_message_reply_markup.assert_not_awaited()  # card keeps its buttons


def test_draft_without_sql(api):
    api.on("GET", "/internal/drafts/7", json={"audit_id": 7, "status": "pending",
                                              "draft_sql": None, "final_sql": None})
    update = make_update(callback_data="sql:7")
    run(bot.button_callback(update, make_context()))
    assert popup(update) == "No SQL for this one."
    update.callback_query.answer.assert_awaited_once()


def test_answer_sql_button(api):
    api.on("GET", "/internal/queries/9", json={"query_id": 9, "sql": "SELECT 1", "source": "template"})
    update = make_update(callback_data="qsql:9")
    run(bot.button_callback(update, make_context()))
    assert query_replies(update) == ["🔍 <b>SQL used (built-in template)</b>\n<pre>SELECT 1</pre>"]
    assert buttons(markup_of(update.callback_query.message.reply_text)) == [("✖ Close", "close:0")]
    update.callback_query.edit_message_reply_markup.assert_not_awaited()  # can be opened again


def test_close_removes_the_sql_message(api):
    from unittest.mock import AsyncMock

    update = make_update(callback_data="close:0")
    update.callback_query.message.delete = AsyncMock()
    run(bot.button_callback(update, make_context()))
    update.callback_query.message.delete.assert_awaited_once()
    update.callback_query.answer.assert_awaited_once()
    assert api.requests == []


def test_close_falls_back_when_telegram_refuses_to_delete(api):
    from unittest.mock import AsyncMock

    update = make_update(callback_data="close:0")
    update.callback_query.message.delete = AsyncMock(side_effect=RuntimeError("too old"))
    run(bot.button_callback(update, make_context()))
    assert edited(update) == "🔍 SQL closed."


def test_answer_sql_gone(api):
    api.on("GET", "/internal/queries/9", status=404, json={"detail": "Query not found"})
    update = make_update(callback_data="qsql:9")
    run(bot.button_callback(update, make_context()))
    assert "don't have the SQL" in popup(update)


@pytest.mark.parametrize("data", ["garbage", "save:abc", "explode:7"])
def test_invalid_buttons(api, data):
    update = make_update(callback_data=data)
    run(bot.button_callback(update, make_context()))
    assert edited(update) == "That button is no longer valid." and api.requests == []


def test_buttons_from_older_cards_still_work(api):
    api.on("POST", "/internal/execute", json={"status": "already_executed"})
    update = make_update(callback_data="approve:7")
    run(bot.button_callback(update, make_context()))
    assert edited(update) == "✅ Already saved."


@pytest.mark.parametrize("data,method,path,reply", [
    ("save:7", "POST", "/internal/execute", {"status": "already_executed"}),
    ("cancel:7", "POST", "/internal/discard", {"status": "discarded"}),
    ("undo:3", "POST", "/internal/undo", {"status": "undone", "preview": "x"}),
    ("create:8", "POST", "/internal/approve_habit", CARD),
    ("nocreate:8", "POST", "/internal/approve_habit", {}),
    ("unit:7:km", "POST", "/internal/clarify", CARD),
    ("fix:7:today", "POST", "/internal/feedback", CARD),
    ("sql:7", "GET", "/internal/drafts/7", {"status": "pending", "draft_sql": "X"}),
    ("qsql:9", "GET", "/internal/queries/9", {"sql": "S"}),
    ("change:7", None, None, None),
    ("keep:7", None, None, None),
])
def test_every_button_answers_exactly_once(api, data, method, path, reply):
    if path:
        api.on(method, path, json=reply)
    update = make_update(callback_data=data)
    run(bot.button_callback(update, make_context()))
    update.callback_query.answer.assert_awaited_once()


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
def test_cancel_clears_everything_and_drops_a_unit_question(api):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    context = make_context(**{bot.UNIT: {"audit_id": 7, "message_id": 70},
                              bot.EDIT: {"audit_id": 9, "card_message_id": 1,
                                         "prompt_message_id": 2}})
    update = make_update("/cancel")
    run(bot.cancel_cmd(update, context))
    assert context.user_data == {} and replies(update) == ["OK, cancelled."]
    assert body(api) == {"audit_id": 7}


def test_cancel_with_nothing_open(api):
    update = make_update("/cancel")
    run(bot.cancel_cmd(update, make_context()))
    assert replies(update) == ["There's nothing to cancel."]


def test_undo_command(api):
    api.on("POST", "/internal/undo", json={"status": "undone", "preview": "4 miles of Running today"})
    update = make_update("/undo")
    run(bot.undo_cmd(update, make_context()))
    assert body(api) == {} and replies(update) == ["↩️ <b>Undone</b>: 4 miles of Running today"]


def test_undo_command_nothing_to_undo(api):
    api.on("POST", "/internal/undo", status=404, json={"detail": "Nothing to undo"})
    update = make_update("/undo")
    run(bot.undo_cmd(update, make_context()))
    assert replies(update) == ["Nothing to undo."]


def test_today_command(api):
    api.on("GET", "/internal/today", json={"logs": [
        {"habit": "Running", "amount": 2, "metric": "miles"},
        {"habit": "Running", "amount": 1, "metric": "miles"},
        {"habit": "Running", "amount": 5, "metric": "km"}]})
    update = make_update("/today")
    run(bot.today_cmd(update, make_context()))
    assert replies(update) == ["📅 <b>Today so far</b>\n• <b>Running</b> · 3 miles\n"
                               "• <b>Running</b> · 5 km"]


def test_today_command_empty(api):
    api.on("GET", "/internal/today", json={"logs": []})
    update = make_update("/today")
    run(bot.today_cmd(update, make_context()))
    assert replies(update)[0].startswith("Nothing logged yet today")


def test_habits_command(api):
    api.on("GET", "/internal/habits", json=[{"display_name": "Running", "metric": "miles"},
                                            {"display_name": "Pushups", "metric": None}])
    update = make_update("/habits")
    run(bot.habits_cmd(update, make_context()))
    assert replies(update) == ["📋 <b>Your habits</b>\n• <b>Running</b> · miles\n"
                               "• <b>Pushups</b> · no default unit"]


def test_stats_command(api):
    api.on("GET", "/internal/stats", json=[
        {"habit": "Running", "metric": "miles", "total": 12.5, "days": 4, "streak_days": 3},
        {"habit": "Reading", "metric": "pages", "total": 1, "days": 1, "streak_days": 0}])
    update = make_update("/stats")
    run(bot.stats_cmd(update, make_context()))
    assert replies(update) == ["📊 <b>Last 30 days</b>\n"
                               "• <b>Running</b> · 12.5 miles on 4 days · 🔥 3\n"
                               "• <b>Reading</b> · 1 page on 1 day"]


def test_stats_command_empty(api):
    api.on("GET", "/internal/stats", json=[])
    update = make_update("/stats")
    run(bot.stats_cmd(update, make_context()))
    assert "No logs in the last 30 days" in replies(update)[0]


@pytest.mark.parametrize("handler,path", [
    (bot.today_cmd, "/internal/today"), (bot.habits_cmd, "/internal/habits"),
    (bot.stats_cmd, "/internal/stats"),
])
def test_commands_when_api_is_down(api, handler, path):
    api.on("GET", path, raises=httpx.ConnectError("down"))
    update = make_update("/x")
    run(handler(update, make_context()))
    assert replies(update) == [bot.UNREACHABLE]


def test_start_and_help(api):
    update = make_update("/start")
    run(bot.start(update, make_context()))
    assert "HabitFlow is ready" in replies(update)[0]
    assert "how much did I read this month?" in replies(update)[0]
    update = make_update("/help")
    run(bot.help_cmd(update, make_context()))
    assert replies(update) == [bot.HELP_TEXT]


def test_startup_registers_the_command_menu_only():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))
    run(bot.on_startup(app))
    names = [c.command for c in app.bot.set_my_commands.await_args.args[0]]
    assert names == [n for n, _ in bot.COMMANDS] and "remind" not in names
