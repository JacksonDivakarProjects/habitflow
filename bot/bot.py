import logging
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    telegram_allowed_user_id: int
    api_base_url: str = "http://api:8000"
    # Calls that hit the LLM: API worst case is 3 retries x LLM_TIMEOUT_SECONDS (20s).
    api_llm_timeout_seconds: float = 90
    app_timezone: str = "Asia/Kolkata"  # reminder times are in this zone


settings = Settings()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("habitflow.bot")

EXAMPLES = "“ran 3 miles” or “read 20 pages yesterday”"
COULDNT_UNDERSTAND = f"Sorry, I couldn't turn that into a log. Try something like {EXAMPLES}."
UNREACHABLE = "I can't reach the server right now. Please try again in a moment."

COMMANDS = [
    ("today", "What you've logged today"),
    ("stats", "Last 30 days and streaks"),
    ("undo", "Undo your last log"),
    ("habits", "Your habits and their units"),
    ("remind", "Daily check-in, e.g. /remind 21:00"),
    ("cancel", "Cancel the current question"),
    ("help", "How to use HabitFlow"),
]

# Typed instead of an answer to a question, these mean /cancel.
CANCEL_WORDS = {"cancel", "never mind", "nevermind", "nvm", "stop", "forget it", "skip"}


def _is_cancel(text: str | None) -> bool:
    return (text or "").strip().lower().rstrip("!.") in CANCEL_WORDS


def authorized(update: Update) -> bool:
    if update.effective_user is None:
        return False
    return update.effective_user.id == settings.telegram_allowed_user_id


def _escape_md(s: str) -> str:
    if not s:
        return s
    for ch in ("_", "*", "[", "]", "`"):
        s = s.replace(ch, "\\" + ch)
    return s


def _detail(r: httpx.Response):
    try:
        return r.json().get("detail")
    except Exception:
        return None


def _friendly_error(r: httpx.Response) -> str:
    """Turn an API error into something a person can act on. Never raw JSON."""
    try:
        detail = str(r.json().get("detail", ""))
    except Exception:
        detail = ""
    log.warning("API %s: %s", r.status_code, detail or r.text)
    if r.status_code == 404:
        return "I can't find that anymore. Send your log again."
    if r.status_code == 409:
        if "superseded" in detail:
            return "This draft was replaced by a newer one."
        if "executed" in detail:
            return "That's already logged. ✅"
        if "cancelled" in detail:
            return "This draft was discarded."
        return "This draft is no longer active. Send your log again."
    if r.status_code == 422:
        return COULDNT_UNDERSTAND
    return "Something went wrong on my side. Please try again in a moment."


async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show "typing…" while the LLM works. Cosmetic: never let it fail a request."""
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
    except Exception:
        pass


def _card_markup(audit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{audit_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"feedback:{audit_id}"),
                InlineKeyboardButton("🗑️ Discard", callback_data=f"discard:{audit_id}"),
            ]
        ]
    )


def _undo_markup(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Undo", callback_data=callback_data)]]
    )


def _format_card(
    audit_id: int,
    preview: str,
    metric_source: str,
    draft_sql: str | None,
    offline: bool = False,
    skipped: list[str] | None = None,
) -> str:
    lines = preview.splitlines() or [preview]
    suffix = " _(suggested unit)_" if metric_source == "suggested" else ""
    body = "\n".join(
        f"📝 {_escape_md(line)}{suffix if i == 0 else ''}" for i, line in enumerate(lines)
    )
    if skipped:
        body += "\n\n⚠️ Not included: " + _escape_md("; ".join(skipped))
    if offline:
        body += "\n\n⚡ _AI is offline, so this was read by the simple parser. Please check it._"
    body += f"\n\n_Draft #{audit_id}_"
    if draft_sql:
        body += f"\n\n```sql\n{draft_sql.strip()}\n```"
    return body


def _card_text(data: dict) -> str:
    return _format_card(
        data["audit_id"],
        data["preview"],
        data.get("metric_source", "explicit"),
        data.get("draft_sql"),
        offline=data.get("offline", False),
        skipped=data.get("skipped"),
    )


_SINGULAR = {  # units are stored plural; mirrors api/app/units.py
    "miles": "mile", "meters": "meter", "minutes": "minute", "hours": "hour",
    "seconds": "second", "pages": "page", "books": "book", "chapters": "chapter",
    "steps": "step", "reps": "rep", "sets": "set", "concepts": "concept",
    "glasses": "glass", "liters": "liter", "calories": "calorie", "laps": "lap",
}


def _qty(amount: float, unit: str) -> str:
    """'1 mile', '2 miles'."""
    return f"{amount:g} {_SINGULAR.get(unit, unit) if amount == 1 else unit}"


def _format_logged(summary: dict) -> str:
    s = summary
    lines = [f"✅ Logged {_qty(s['amount'], s['metric'])} of {s['habit']} {s['when']}"]
    streak = s.get("streak_days") or 0
    parts = []
    if streak >= 2:
        parts.append(f"🔥 {streak}-day streak")
    elif streak == 1:
        parts.append("🌱 Day 1 of a new streak")
    parts.append(f"{_qty(s['week_total'], s['metric'])} this week")
    lines.append(" · ".join(parts))
    return "\n".join(lines)


def _format_executed(data: dict) -> str:
    summaries = data.get("summaries") or [data["summary"]]
    return "\n\n".join(_format_logged(s) for s in summaries)


def format_reminder(check: dict) -> str | None:
    """Evening check-in text, or None when there's nothing worth saying."""
    if not check["at_risk"] and not check["not_logged"]:
        return None
    lines = ["⏰ Evening check-in"]
    for item in check["at_risk"]:
        days = item["streak_days"]
        if days > 1:
            lines.append(f"🔥 {item['habit']}: log it today to keep your {days}-day streak going.")
        else:
            lines.append(
                f"🌱 {item['habit']}: you started yesterday. Log it today to make it 2 days."
            )
    if check["not_logged"]:
        lines.append("Not logged yet today: " + ", ".join(check["not_logged"]) + ".")
    if check.get("done"):
        lines.append("✅ Done today: " + ", ".join(check["done"]) + ".")
    lines.append("Just reply here, like “ran 3 miles”.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "👋 HabitFlow is ready.\n\n"
        f"Just tell me what you did, like {EXAMPLES}. "
        "I'll show you a draft, and nothing is saved until you tap ✅.\n\n"
        "Try /today, /stats or /help. Want an evening nudge? /remind 21:00"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Tell me what you did in plain words:\n"
        "  “ran 4 miles”\n"
        "  “read 20 pages yesterday”\n"
        "  “meditated 10 min”\n"
        "  “learned rust for 2 hours” (new habits are created for you)\n"
        "  “ran 3 miles and read 20 pages” (several at once)\n\n"
        "Each draft card has three buttons:\n"
        "  ✅ Approve saves it\n"
        "  ✏️ Edit lets you reply with a fix, like “6 miles, not 4”\n"
        "  🗑️ Discard throws it away\n"
        "After saving you can tap ↩️ Undo, or send /undo later.\n"
        "Answering a question? Reply “cancel” to drop it.\n\n"
        "Commands:\n"
        + "\n".join(f"  /{name} — {desc}" for name, desc in COMMANDS)
    )


async def habits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.api_base_url}/internal/habits")
    except Exception:
        log.exception("habits call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return
    habits = r.json()
    if not habits:
        await update.message.reply_text(
            f"No habits yet. Log something like {EXAMPLES} and I'll create it."
        )
        return
    lines = [
        f"• {h['display_name']} ({h.get('metric') or 'no default unit'})" for h in habits
    ]
    await update.message.reply_text("Tracked habits:\n" + "\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.api_base_url}/internal/stats",
                params={"chat_id": update.effective_chat.id},
            )
    except Exception:
        log.exception("stats call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return
    rows = r.json()
    if not rows:
        await update.message.reply_text("No logs in the last 30 days. Send one to get started!")
        return
    lines = ["📊 Last 30 days:"]
    for row in rows:
        days = row["days"]
        line = (
            f"• {row['habit']}: {_qty(row['total'], row['metric'])} "
            f"on {days} day{'s' if days != 1 else ''}"
        )
        if row.get("streak_days", 0) >= 2:
            line += f" · 🔥 {row['streak_days']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.api_base_url}/internal/today")
    except Exception:
        log.exception("today call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return
    logs = r.json()["logs"]
    if not logs:
        await update.message.reply_text(
            f"Nothing logged yet today. Send something like {EXAMPLES}."
        )
        return
    totals: dict[tuple[str, str], float] = {}
    for item in logs:
        key = (item["habit"], item["metric"])
        totals[key] = totals.get(key, 0) + item["amount"]
    lines = ["📅 Today so far:"] + [
        f"• {habit}: {_qty(amount, metric)}" for (habit, metric), amount in totals.items()
    ]
    await update.message.reply_text("\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{settings.api_base_url}/internal/undo", json={})
    except Exception:
        log.exception("undo call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code == 404:
        await update.message.reply_text("Nothing to undo.")
        return
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return
    await update.message.reply_text(f"↩️ Undid your last log: {r.json()['preview']}.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    popped = [  # a list, not any(<generator>): every key must be popped
        context.user_data.pop(key, None)
        for key in ("awaiting_clarification", "awaiting_feedback_for", "feedback_card_message_id")
    ]
    had_question = any(value is not None for value in popped)
    await update.message.reply_text(
        "OK, cancelled." if had_question else "There's nothing to cancel."
    )


async def _send_card(update: Update, data: dict):
    body = _card_text(data)
    markup = _card_markup(data["audit_id"])
    try:
        await update.message.reply_text(body, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(f"📝 {data['preview']}", reply_markup=markup)


async def _send_habit_card(update: Update, audit_id: int, prompt: str):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Create & log", callback_data=f"create_habit:{audit_id}"),
                InlineKeyboardButton("❌ No thanks", callback_data=f"cancel_habit:{audit_id}"),
            ]
        ]
    )
    await update.message.reply_text(prompt, reply_markup=keyboard)


async def _handle_draft_result(update: Update, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """Route a DraftResponse: ask for a unit, offer a new habit, or show the card."""
    if data.get("needs_input") == "unit":
        context.user_data["awaiting_clarification"] = {"audit_id": data["audit_id"]}
        await update.message.reply_text(data["prompt"])
        return
    if data.get("needs_input") == "habit":
        await _send_habit_card(update, data["audit_id"], data["prompt"])
        return
    await _send_card(update, data)


async def _retire_card(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id):
    if not message_id:
        return
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id,
            text="✏️ Changed. See my next message.",
        )
    except Exception:
        log.warning("could not retire card %s", message_id)


async def _send_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, audit_id: int, feedback: str
):
    card_message_id = context.user_data.pop("feedback_card_message_id", None)
    await _typing(update, context)
    try:
        async with httpx.AsyncClient(timeout=settings.api_llm_timeout_seconds) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/feedback",
                json={"audit_id": audit_id, "feedback": feedback},
            )
    except Exception:
        log.exception("feedback call failed")
        # Keep the edit open: resending the fix should retry it, not start a new draft.
        context.user_data["awaiting_feedback_for"] = audit_id
        if card_message_id:
            context.user_data["feedback_card_message_id"] = card_message_id
        await update.message.reply_text(f"{UNREACHABLE} Send your fix again, or “cancel”.")
        return

    if r.status_code == 422:
        # Keep the edit open so the next message is another try.
        context.user_data["awaiting_feedback_for"] = audit_id
        if card_message_id:
            context.user_data["feedback_card_message_id"] = card_message_id
        await update.message.reply_text(
            "I couldn't apply that change. Try rephrasing, like “6 miles, not 4”, "
            "or /cancel."
        )
        return
    if r.status_code >= 400:
        await update.message.reply_text(_friendly_error(r))
        return

    data = r.json()
    if data.get("needs_input"):
        # The old card's buttons no longer apply: the draft now needs an answer.
        await _retire_card(update, context, card_message_id)
        await _handle_draft_result(update, context, data)
        return

    # Regenerated: update the original card in place.
    body = _card_text(data)
    if card_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=card_message_id,
                text=f"🔄 Updated\n\n{body}",
                reply_markup=_card_markup(data["audit_id"]),
                parse_mode="Markdown",
            )
            return
        except Exception:
            log.warning("could not edit card %s in place", card_message_id)
    await _send_card(update, data)


async def _send_clarification(
    update: Update, context: ContextTypes.DEFAULT_TYPE, audit_id: int
):
    await _typing(update, context)
    try:
        async with httpx.AsyncClient(timeout=settings.api_llm_timeout_seconds) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/clarify",
                json={"audit_id": audit_id, "value": update.message.text},
            )
    except Exception:
        log.exception("clarify call failed")
        await update.message.reply_text(f"{UNREACHABLE} Reply again, or /cancel.")
        return

    detail = _detail(r)
    if r.status_code == 409 and isinstance(detail, dict) and detail.get("code") == "new_log":
        # Not a unit but a new log ("read 20 pages"): drop the question, draft this.
        context.user_data.pop("awaiting_clarification", None)
        await update.message.reply_text(
            f"OK, I dropped “{detail.get('dropped', 'the earlier one')}” (no unit) "
            "and read this as a new log."
        )
        await _draft_new(update, context)
        return
    if r.status_code in (404, 409):
        # The audit is gone or no longer waiting on us; stop routing here.
        context.user_data.pop("awaiting_clarification", None)
        await update.message.reply_text("That question has expired. Send your log again.")
        return
    if r.status_code >= 400:
        await update.message.reply_text(f"{_friendly_error(r)} Reply again, or /cancel.")
        return

    data = r.json()
    if data.get("needs_input"):
        await update.message.reply_text(data["prompt"])
        return
    context.user_data.pop("awaiting_clarification", None)
    await _send_card(update, data)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    pending_feedback = context.user_data.pop("awaiting_feedback_for", None)
    if pending_feedback is not None:
        if _is_cancel(update.message.text):
            context.user_data.pop("feedback_card_message_id", None)
            await update.message.reply_text("OK, I left the draft as it was.")
            return
        await _send_feedback(update, context, pending_feedback, update.message.text)
        return

    awaiting = context.user_data.get("awaiting_clarification")
    if awaiting is not None:
        if _is_cancel(update.message.text):
            context.user_data.pop("awaiting_clarification", None)
            try:
                await _post("/internal/discard", {"audit_id": awaiting["audit_id"]})
            except Exception:
                log.exception("discard after cancel failed")
            await update.message.reply_text("OK, dropped it.")
            return
        await _send_clarification(update, context, awaiting["audit_id"])
        return

    await _draft_new(update, context)


async def _draft_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _typing(update, context)
    try:
        async with httpx.AsyncClient(timeout=settings.api_llm_timeout_seconds) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/draft",
                json={
                    "chat_id": update.effective_chat.id,
                    "text": update.message.text,
                    "message_id": update.message.message_id,
                },
            )
    except Exception:
        log.exception("draft call failed")
        await update.message.reply_text(UNREACHABLE)
        return

    if r.status_code >= 400:
        await update.message.reply_text(_friendly_error(r))
        return
    await _handle_draft_result(update, context, r.json())


async def _post(path: str, payload: dict, timeout: float = 30) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(f"{settings.api_base_url}{path}", json=payload)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    if update.effective_user.id != settings.telegram_allowed_user_id:
        await query.answer()
        return

    try:
        action, id_str = query.data.split(":", 1)
        target_id = int(id_str)
    except Exception:
        await query.answer()
        await query.edit_message_text("Malformed button.")
        return

    # Acting on a card closes any edit that was open for it.
    editing = context.user_data.get("awaiting_feedback_for")
    if action in ("approve", "discard") and editing == target_id:
        context.user_data.pop("awaiting_feedback_for", None)
        context.user_data.pop("feedback_card_message_id", None)

    if action == "feedback":
        await query.answer()
        context.user_data["awaiting_feedback_for"] = target_id
        context.user_data["feedback_card_message_id"] = query.message.message_id
        await query.edit_message_text(
            f"{query.message.text}\n\n"
            "✏️ What should change? Reply with the fix, like “6 miles, not 4” "
            "or “it was yesterday”. Reply “cancel” to keep it as is.",
            reply_markup=_card_markup(target_id),
        )
        return

    endpoints = {
        "approve": ("/internal/execute", {"audit_id": target_id}),
        "discard": ("/internal/discard", {"audit_id": target_id}),
        "undo": ("/internal/undo", {"log_id": target_id}),
        "undo_audit": ("/internal/undo", {"audit_id": target_id}),
        "create_habit": ("/internal/approve_habit", {"audit_id": target_id, "accept": True}),
        "cancel_habit": ("/internal/approve_habit", {"audit_id": target_id, "accept": False}),
    }
    if action not in endpoints:
        await query.answer()
        await query.edit_message_text("Malformed button.")
        return

    path, payload = endpoints[action]
    try:
        r = await _post(path, payload)
    except Exception:
        log.exception("%s call failed", action)
        await query.answer(UNREACHABLE, show_alert=True)
        return

    if r.status_code >= 400:
        # Leave the card alone; show why in a popup.
        await query.answer(_friendly_error(r), show_alert=True)
        return
    await query.answer()
    data = r.json()

    if action == "approve":
        if data.get("status") == "already_executed":
            await query.edit_message_text("✅ Already logged.")
            return
        several = len(data.get("log_ids") or []) > 1
        undo = f"undo_audit:{target_id}" if several else f"undo:{data['log_id']}"
        await query.edit_message_text(_format_executed(data), reply_markup=_undo_markup(undo))
    elif action == "discard":
        await query.edit_message_text("🗑️ Discarded.")
    elif action in ("undo", "undo_audit"):
        if data.get("status") == "already_undone":
            await query.edit_message_text(f"↩️ Already undone: {data['preview']}.")
        else:
            await query.edit_message_text(f"↩️ Undone: {data['preview']}.")
    elif action == "create_habit":
        if data.get("needs_input") == "unit":
            context.user_data["awaiting_clarification"] = {"audit_id": target_id}
            await query.edit_message_text(data["prompt"])
            return
        body = _card_text(data)
        try:
            await query.edit_message_text(
                f"✨ New habit created.\n\n{body}",
                reply_markup=_card_markup(target_id),
                parse_mode="Markdown",
            )
        except Exception:
            await query.edit_message_text(
                f"✨ New habit created.\n\n📝 {data['preview']}",
                reply_markup=_card_markup(target_id),
            )
    elif action == "cancel_habit":
        await query.edit_message_text("OK, I didn't create it.")


# ------------------------------------------------------------------
# Reminders
# ------------------------------------------------------------------
def _job_name(chat_id: int) -> str:
    return f"reminder:{chat_id}"


def unschedule_reminder(job_queue, chat_id: int):
    for job in job_queue.get_jobs_by_name(_job_name(chat_id)):
        job.schedule_removal()


def schedule_reminder(job_queue, chat_id: int, remind_at: str):
    """(Re)schedule the daily check-in at "HH:MM" in the app timezone."""
    unschedule_reminder(job_queue, chat_id)
    hour, minute = (int(x) for x in remind_at.split(":"))
    job_queue.run_daily(
        send_reminder,
        time=dtime(hour, minute, tzinfo=ZoneInfo(settings.app_timezone)),
        chat_id=chat_id,
        name=_job_name(chat_id),
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.api_base_url}/internal/reminders/check", params={"chat_id": chat_id}
            )
        r.raise_for_status()
    except Exception:
        log.exception("reminder check failed")
        return  # a missed nudge is better than an error message at 9pm
    text = format_reminder(r.json())
    if text:
        await context.bot.send_message(chat_id=chat_id, text=text)


async def _put_reminder(payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.put(f"{settings.api_base_url}/internal/reminders", json=payload)


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    arg = " ".join(context.args or []).strip().lower()
    job_queue = context.job_queue
    try:
        if not arg:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{settings.api_base_url}/internal/reminders")
            mine = next((x for x in r.json() if x["chat_id"] == chat_id), None)
            if mine and mine["enabled"]:
                await update.message.reply_text(
                    f"⏰ Reminders are on at {mine['remind_at']}. "
                    "/remind 20:30 changes the time, /remind off stops them."
                )
            else:
                await update.message.reply_text(
                    "Reminders are off. Try /remind 21:00 or /remind 9pm "
                    "for a daily evening check-in."
                )
            return
        if arg in ("off", "stop", "no", "disable"):
            await _put_reminder({"chat_id": chat_id, "remind_at": None, "enabled": False})
            if job_queue:
                unschedule_reminder(job_queue, chat_id)
            await update.message.reply_text("🔕 Reminders are off.")
            return
        r = await _put_reminder({"chat_id": chat_id, "remind_at": arg, "enabled": True})
    except Exception:
        log.exception("reminder settings call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code == 422:
        await update.message.reply_text(
            "I didn't understand that time. Try /remind 21:00 or /remind 9pm."
        )
        return
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return
    remind_at = r.json()["remind_at"]
    if job_queue:
        schedule_reminder(job_queue, chat_id, remind_at)
    await update.message.reply_text(
        f"⏰ Done. I'll check in every day at {remind_at} ({settings.app_timezone}), "
        "but only if something still needs logging."
    )


REMINDER_RETRY_SECONDS = 30


async def load_reminders(job_queue) -> bool:
    """Schedule every enabled reminder stored in the API. False if unreachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.api_base_url}/internal/reminders")
        r.raise_for_status()
        reminders = r.json()
    except Exception as e:
        log.warning("could not load reminders (retrying in %ss): %s", REMINDER_RETRY_SECONDS, e)
        return False
    for item in reminders:
        if item["enabled"]:
            schedule_reminder(job_queue, item["chat_id"], item["remind_at"])
            log.info("reminder scheduled for %s at %s", item["chat_id"], item["remind_at"])
    return True


async def _retry_load_reminders(context: ContextTypes.DEFAULT_TYPE):
    if not await load_reminders(context.job_queue):
        context.job_queue.run_once(
            _retry_load_reminders, REMINDER_RETRY_SECONDS, name="reminder-sync"
        )


async def on_startup(app: Application):
    try:
        await app.bot.set_my_commands([BotCommand(n, d) for n, d in COMMANDS])
    except Exception as e:
        log.warning("could not set command menu: %s", e)
    if app.job_queue is None:
        log.warning("job queue unavailable; install python-telegram-bot[job-queue]")
        return
    # The API may still be starting (no startup ordering on some hosts):
    # keep trying in the background instead of silently scheduling nothing.
    if not await load_reminders(app.job_queue):
        app.job_queue.run_once(_retry_load_reminders, REMINDER_RETRY_SECONDS, name="reminder-sync")


def main():
    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("habits", habits_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("remind", remind_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
