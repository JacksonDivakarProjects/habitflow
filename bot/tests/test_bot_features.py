"""Multi-habit cards, the Edit flow's edge cases, "cancel" words and reminders."""

import asyncio
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

import bot
from tests.conftest import CHAT, FakeJobQueue, edited, make_context, make_update, replies

MULTI = {
    "audit_id": 7,
    "preview": "3 miles of Running today\n20 pages of Reading today",
    "metric_source": "suggested",
    "draft_sql": None,
    "offline": False,
    "skipped": [],
}
SUMMARY = {
    "log_id": 3, "habit": "Running", "amount": 3.0, "metric": "miles",
    "log_date": "2026-09-23", "when": "today", "streak_days": 2, "week_total": 3.0,
}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------
# Several habits in one card
# ------------------------------------------------------------------
def test_multi_card_puts_each_log_on_its_own_line():
    body = bot._card_text(MULTI)

    assert body.startswith(
        "📝 3 miles of Running today _(suggested unit)_\n📝 20 pages of Reading today"
    )


def test_card_lists_what_was_left_out():
    body = bot._card_text({**MULTI, "skipped": ["Pushups (new habit, send it on its own)"]})

    assert "⚠️ Not included: Pushups (new habit, send it on its own)" in body


def test_approving_multi_card_shows_each_log_and_one_undo_for_all(api):
    second = {**SUMMARY, "log_id": 4, "habit": "Reading", "amount": 20.0, "metric": "pages",
              "streak_days": 1, "week_total": 20.0}
    api.on("POST", "/internal/execute", json={
        "status": "executed", "log_id": 3, "log_ids": [3, 4],
        "summary": SUMMARY, "summaries": [SUMMARY, second],
    })
    update = make_update(callback_data="approve:7")

    run(bot.button_callback(update, make_context()))

    call = update.callback_query.edit_message_text.await_args
    assert call.args[0] == (
        "✅ Logged 3 miles of Running today\n🔥 2-day streak · 3 miles this week\n\n"
        "✅ Logged 20 pages of Reading today\n🌱 Day 1 of a new streak · 20 pages this week"
    )
    assert call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "undo_audit:7"


def test_undo_all_button_undoes_the_whole_draft(api):
    api.on("POST", "/internal/undo", json={
        "status": "undone", "log_id": 3, "log_ids": [3, 4],
        "preview": "3 miles of Running today and 20 pages of Reading today",
    })
    update = make_update(callback_data="undo_audit:7")

    run(bot.button_callback(update, make_context()))

    assert api.last_json() == {"audit_id": 7}
    assert edited(update) == "↩️ Undone: 3 miles of Running today and 20 pages of Reading today."


def test_draft_asking_for_unit_mentions_the_rest(api):
    api.on("POST", "/internal/draft", json={
        "audit_id": 7, "needs_input": "unit",
        "prompt": "Got it: 3 of Running today. What unit? (e.g. miles) (+1 more log in this message)",
    })
    update = make_update("ran 3 and read 20 pages")

    run(bot.handle_text(update, make_context()))

    assert replies(update)[0].endswith("(+1 more log in this message)")


# ------------------------------------------------------------------
# The Edit flow
# ------------------------------------------------------------------
def test_edit_that_turns_into_a_question_retires_the_old_card(api):
    api.on("POST", "/internal/feedback", json={
        "audit_id": 7, "needs_input": "habit", "prompt": "“Swimming” is a new habit. Create it?",
    })
    update = make_update("no, it was swimming")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    retired = context.bot.edit_message_text.await_args.kwargs
    assert (retired["message_id"], retired["text"]) == (99, "✏️ Changed. See my next message.")
    assert "reply_markup" not in retired  # dead buttons removed
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "create_habit:7"


def test_edit_that_needs_a_unit_waits_for_it(api):
    api.on("POST", "/internal/feedback", json={
        "audit_id": 7, "needs_input": "unit", "prompt": "What unit?",
    })
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(make_update("not miles"), context))

    assert context.user_data == {"awaiting_clarification": {"audit_id": 7}}


def test_edit_in_place_falls_back_to_new_message(api):
    api.on("POST", "/internal/feedback", json={**MULTI, "preview": "6 miles of Running today"})
    update = make_update("6")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)
    context.bot.edit_message_text.side_effect = RuntimeError("message is too old to edit")

    run(bot.handle_text(update, context))

    assert "6 miles of Running today" in replies(update)[0]


def test_edit_without_a_known_card_sends_new_card(api):
    api.on("POST", "/internal/feedback", json={**MULTI, "preview": "6 miles of Running today"})
    update = make_update("6")
    context = make_context(awaiting_feedback_for=7)

    run(bot.handle_text(update, context))

    context.bot.edit_message_text.assert_not_awaited()
    assert "6 miles of Running today" in replies(update)[0]


def test_edit_shows_typing(api):
    api.on("POST", "/internal/feedback", json=MULTI)
    context = make_context(awaiting_feedback_for=7)

    run(bot.handle_text(make_update("6"), context))

    context.bot.send_chat_action.assert_awaited()


def test_edit_api_unreachable(api):
    api.on("POST", "/internal/feedback", raises=httpx.ConnectError("down"))
    update = make_update("6")

    run(bot.handle_text(update, make_context(awaiting_feedback_for=7)))

    assert replies(update)[0].startswith(bot.UNREACHABLE)


def test_tapping_edit_on_another_card_switches_target(api):
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.button_callback(make_update(callback_data="feedback:8", message_id=100), context))

    assert context.user_data == {"awaiting_feedback_for": 8, "feedback_card_message_id": 100}


# ------------------------------------------------------------------
# "cancel" typed as an answer
# ------------------------------------------------------------------
@pytest.mark.parametrize("word", ["cancel", "Cancel!", "never mind", "nvm", "forget it."])
def test_cancel_word_during_edit_keeps_draft(api, word):
    update = make_update(word)
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert api.requests == []  # never sent to the LLM as a correction
    assert context.user_data == {}
    assert replies(update) == ["OK, I left the draft as it was."]


def test_cancel_word_during_unit_question_drops_the_draft(api):
    api.on("POST", "/internal/discard", json={"status": "discarded"})
    update = make_update("cancel")
    context = make_context(awaiting_clarification={"audit_id": 7})

    run(bot.handle_text(update, context))

    assert api.last_json() == {"audit_id": 7}
    assert context.user_data == {}
    assert replies(update) == ["OK, dropped it."]


def test_sentence_containing_cancel_is_still_a_correction(api):
    api.on("POST", "/internal/feedback", json=MULTI)
    context = make_context(awaiting_feedback_for=7)

    run(bot.handle_text(make_update("cancel the reading part"), context))

    assert api.last_json()["feedback"] == "cancel the reading part"


# ------------------------------------------------------------------
# Reminder text
# ------------------------------------------------------------------
def test_format_reminder_full():
    text = bot.format_reminder({
        "at_risk": [{"habit": "Running", "streak_days": 5}, {"habit": "Reels", "streak_days": 1}],
        "not_logged": ["Meditation"],
        "done": ["Reading"],
    })

    assert text.splitlines() == [
        "⏰ Evening check-in",
        "🔥 Running: log it today to keep your 5-day streak going.",
        "🌱 Reels: you started yesterday. Log it today to make it 2 days.",
        "Not logged yet today: Meditation.",
        "✅ Done today: Reading.",
        "Just reply here, like “ran 3 miles”.",
    ]


def test_format_reminder_stays_quiet_when_everything_is_done():
    assert bot.format_reminder({"at_risk": [], "not_logged": [], "done": ["Running"]}) is None


def _job_context(chat_id=CHAT):
    return SimpleNamespace(
        job=SimpleNamespace(chat_id=chat_id), bot=SimpleNamespace(send_message=AsyncMock())
    )


def test_send_reminder(api):
    api.on("GET", "/internal/reminders/check", json={
        "at_risk": [], "not_logged": ["Meditation"], "done": [],
    })
    context = _job_context()

    run(bot.send_reminder(context))

    kwargs = context.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == CHAT
    assert "Not logged yet today: Meditation." in kwargs["text"]


def test_send_reminder_nothing_to_say(api):
    api.on("GET", "/internal/reminders/check", json={"at_risk": [], "not_logged": [], "done": []})
    context = _job_context()

    run(bot.send_reminder(context))

    context.bot.send_message.assert_not_awaited()


def test_send_reminder_api_down_is_silent(api):
    api.on("GET", "/internal/reminders/check", status=500, json={})
    context = _job_context()

    run(bot.send_reminder(context))

    context.bot.send_message.assert_not_awaited()


# ------------------------------------------------------------------
# Scheduling and /remind
# ------------------------------------------------------------------
def test_schedule_reminder_uses_app_timezone_and_replaces_old_job():
    jq = FakeJobQueue()

    bot.schedule_reminder(jq, CHAT, "21:00")
    bot.schedule_reminder(jq, CHAT, "20:30")

    [job] = jq.active()
    assert job.kwargs["time"] == time(20, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert (job.name, job.kwargs["chat_id"]) == (f"reminder:{CHAT}", CHAT)
    assert job.kwargs["callback"] is bot.send_reminder


def test_remind_sets_time_and_schedules(api):
    api.on("PUT", "/internal/reminders", json={"chat_id": CHAT, "remind_at": "21:00", "enabled": True})
    update, context = make_update("/remind 9pm"), make_context(args=["9pm"])

    run(bot.remind_cmd(update, context))

    assert api.last_json() == {"chat_id": CHAT, "remind_at": "9pm", "enabled": True}
    assert [j.kwargs["time"].hour for j in context.job_queue.active()] == [21]
    assert replies(update)[0].startswith("⏰ Done. I'll check in every day at 21:00")


def test_remind_bad_time(api):
    api.on("PUT", "/internal/reminders", status=422, json={"detail": "not a time"})
    update, context = make_update("/remind noon"), make_context(args=["noon"])

    run(bot.remind_cmd(update, context))

    assert "didn't understand that time" in replies(update)[0]
    assert context.job_queue.active() == []


def test_remind_off(api):
    api.on("PUT", "/internal/reminders", json={"chat_id": CHAT, "remind_at": "21:00", "enabled": False})
    jq = FakeJobQueue()
    bot.schedule_reminder(jq, CHAT, "21:00")
    update = make_update("/remind off")

    run(bot.remind_cmd(update, make_context(args=["off"], job_queue=jq)))

    assert api.last_json() == {"chat_id": CHAT, "remind_at": None, "enabled": False}
    assert jq.active() == []
    assert replies(update) == ["🔕 Reminders are off."]


@pytest.mark.parametrize(
    "settings_rows, expected",
    [
        ([{"chat_id": CHAT, "remind_at": "21:00", "enabled": True}], "Reminders are on at 21:00"),
        ([{"chat_id": CHAT, "remind_at": "21:00", "enabled": False}], "Reminders are off"),
        ([], "Reminders are off"),
    ],
)
def test_remind_status(api, settings_rows, expected):
    api.on("GET", "/internal/reminders", json=settings_rows)
    update = make_update("/remind")

    run(bot.remind_cmd(update, make_context()))

    assert expected in replies(update)[0]


def test_remind_api_unreachable(api):
    api.on("PUT", "/internal/reminders", raises=httpx.ConnectError("down"))
    update = make_update("/remind 21:00")

    run(bot.remind_cmd(update, make_context(args=["21:00"])))

    assert replies(update) == [bot.UNREACHABLE]


def test_startup_schedules_enabled_reminders_only(api):
    api.on("GET", "/health", json={"status": "ok"})
    api.on("GET", "/internal/reminders", json=[
        {"chat_id": 1, "remind_at": "21:00", "enabled": True},
        {"chat_id": 2, "remind_at": "08:00", "enabled": False},
    ])
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()), job_queue=FakeJobQueue())

    run(bot.on_startup(app))

    assert [j.kwargs["chat_id"] for j in app.job_queue.active()] == [1]


def test_startup_retries_reminders_while_api_is_down(api):
    api.on("GET", "/internal/reminders", raises=httpx.ConnectError("down"))
    app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()), job_queue=FakeJobQueue())

    run(bot.on_startup(app))  # must not raise

    [retry] = app.job_queue.active()
    assert (retry.name, retry.kwargs["when"]) == ("reminder-sync", bot.REMINDER_RETRY_SECONDS)


def test_reminder_retry_schedules_once_api_is_back(api):
    api.on("GET", "/internal/reminders", json=[{"chat_id": 1, "remind_at": "21:00", "enabled": True}])
    jq = FakeJobQueue()

    run(bot._retry_load_reminders(SimpleNamespace(job_queue=jq)))

    assert [j.name for j in jq.active()] == ["reminder:1"]  # scheduled, no further retry


def test_reminder_retry_keeps_retrying(api):
    api.on("GET", "/internal/reminders", status=503, json={})
    jq = FakeJobQueue()

    run(bot._retry_load_reminders(SimpleNamespace(job_queue=jq)))

    assert [j.name for j in jq.active()] == ["reminder-sync"]


@pytest.mark.parametrize(
    "amount, unit, expected",
    [(1, "miles", "1 mile"), (3.5, "miles", "3.5 miles"), (1, "km", "1 km"), (1, "minutes", "1 minute")],
)
def test_quantities_read_naturally(amount, unit, expected):
    assert bot._qty(amount, unit) == expected


# ------------------------------------------------------------------
# Final review findings
# ------------------------------------------------------------------
def test_edit_survives_a_network_error(api):
    """Finding 4: a timeout used to drop Edit mode, so the resent fix became a new draft."""
    api.on("POST", "/internal/feedback", raises=httpx.ReadTimeout("slow"))
    update = make_update("6 miles, not 4")
    context = make_context(awaiting_feedback_for=7, feedback_card_message_id=99)

    run(bot.handle_text(update, context))

    assert context.user_data == {"awaiting_feedback_for": 7, "feedback_card_message_id": 99}
    assert replies(update)[0].startswith(bot.UNREACHABLE)
    assert "Send your fix again" in replies(update)[0]


def test_new_log_sent_to_a_unit_question_is_drafted(api):
    """Finding 2: "read 20 pages" answering "What unit?" becomes its own log."""
    api.on("POST", "/internal/clarify", status=409,
           json={"detail": {"code": "new_log", "dropped": "ran 5"}})
    api.on("POST", "/internal/draft", json={
        "audit_id": 8, "preview": "20 pages of Reading today",
        "metric_source": "explicit", "draft_sql": None,
    })
    update = make_update("read 20 pages")
    context = make_context(awaiting_clarification={"audit_id": 7})

    run(bot.handle_text(update, context))

    assert context.user_data == {}
    first, card = replies(update)
    assert first == "OK, I dropped “ran 5” (no unit) and read this as a new log."
    assert card.startswith("📝 20 pages of Reading today")
    assert api.last_json()["text"] == "read 20 pages"
