import logging

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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


settings = Settings()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("habitflow.bot")


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


def _card_markup(audit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{audit_id}"),
                InlineKeyboardButton(
                    "✏️ Feedback", callback_data=f"feedback:{audit_id}"
                ),
            ]
        ]
    )


def _format_card(
    audit_id: int, preview: str, metric_source: str, draft_sql: str | None
) -> str:
    suffix = " _(suggested)_" if metric_source == "suggested" else ""
    body = f"📝 {_escape_md(preview)}{suffix}\n\n_Audit #{audit_id}_"
    if draft_sql:
        body += f"\n\n```sql\n{draft_sql.strip()}\n```"
    return body


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "HabitFlow ready.\n\n"
        "Send any text to log a habit.\n"
        "Commands: /help /habits /stats /cancel"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Send natural language like:\n"
        '  "ran 4 miles today"\n'
        '  "read 20"\n'
        '  "learned rust for 2 hours"\n\n'
        "You'll see the SQL the assistant drafted before anything is written.\n"
        "Tap ✏️ Feedback to correct the draft — it'll regenerate in place.\n\n"
        "Commands:\n"
        "  /habits — list tracked habits\n"
        "  /stats  — last 30 days totals\n"
        "  /cancel — cancel an open clarification"
    )


async def habits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.api_base_url}/internal/habits")
        if r.status_code != 200:
            await update.message.reply_text(f"API error {r.status_code}")
            return
        habits = r.json()
        if not habits:
            await update.message.reply_text("No habits yet.")
            return
        lines = [
            f"• {h['display_name']} ({h.get('metric') or 'no default unit'})"
            for h in habits
        ]
        await update.message.reply_text("Tracked habits:\n" + "\n".join(lines))
    except Exception as e:
        log.exception("habits call failed")
        await update.message.reply_text(f"Error: {e}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{settings.api_base_url}/internal/stats",
                params={"chat_id": chat_id},
            )
        if r.status_code != 200:
            await update.message.reply_text(f"API error {r.status_code}")
            return
        rows = r.json()
        if not rows:
            await update.message.reply_text("No logs in the last 30 days.")
            return
        lines = ["Last 30 days:"]
        for row in rows:
            lines.append(
                f"• {row['habit']}: {row['total']:g} {row['metric']} "
                f"over {row['days']} day(s)"
            )
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("stats call failed")
        await update.message.reply_text(f"Error: {e}")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data.pop("awaiting_clarification", None)
    context.user_data.pop("awaiting_feedback_for", None)
    context.user_data.pop("feedback_card_message_id", None)
    await update.message.reply_text("Cancelled.")


async def _send_card(
    update: Update,
    audit_id: int,
    preview: str,
    metric_source: str,
    draft_sql: str | None,
):
    body = _format_card(audit_id, preview, metric_source, draft_sql)
    try:
        await update.message.reply_text(
            body, reply_markup=_card_markup(audit_id), parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(
            f"📝 {preview}\n\nAudit #{audit_id}"
            + (f"\n\nSQL:\n{draft_sql.strip()}" if draft_sql else ""),
            reply_markup=_card_markup(audit_id),
        )


async def _send_habit_card(update: Update, audit_id: int, prompt: str):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Create & Log", callback_data=f"create_habit:{audit_id}"
                ),
                InlineKeyboardButton(
                    "✏️ Cancel", callback_data=f"cancel_habit:{audit_id}"
                ),
            ]
        ]
    )
    await update.message.reply_text(prompt, reply_markup=keyboard)


async def _send_feedback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, audit_id: int, feedback: str
):
    try:
        async with httpx.AsyncClient(timeout=settings.api_llm_timeout_seconds) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/feedback",
                json={"audit_id": audit_id, "feedback": feedback},
            )
    except Exception as e:
        log.exception("feedback call failed")
        await update.message.reply_text(f"Error: {e}")
        return

    if r.status_code == 501:
        await update.message.reply_text("Feedback loop not implemented yet.")
        return

    if r.status_code >= 400:
        await update.message.reply_text(f"API error {r.status_code}: {r.text}")
        return

    data = r.json()

    # Still needs input — either a unit or a habit approval
    if data.get("needs_input") == "unit":
        context.user_data["awaiting_clarification"] = {"audit_id": data["audit_id"]}
        await update.message.reply_text(data["prompt"])
        return
    if data.get("needs_input") == "habit":
        await _send_habit_card(update, data["audit_id"], data["prompt"])
        return

    # Regenerated successfully — edit the original card in place
    card_body = _format_card(
        data["audit_id"],
        data["preview"],
        data.get("metric_source", "explicit"),
        data.get("draft_sql"),
    )
    markup = _card_markup(data["audit_id"])

    card_message_id = context.user_data.pop("feedback_card_message_id", None)
    chat_id = update.effective_chat.id

    if card_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=card_message_id,
                text=card_body,
                reply_markup=markup,
                parse_mode="Markdown",
            )
            return
        except Exception:
            pass

    # Fallback: send as a new message
    try:
        await update.message.reply_text(
            card_body, reply_markup=markup, parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text(
            f"📝 {data['preview']}\n\nAudit #{data['audit_id']}",
            reply_markup=markup,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    pending_feedback = context.user_data.pop("awaiting_feedback_for", None)
    if pending_feedback is not None:
        await _send_feedback(update, context, pending_feedback, update.message.text)
        return

    awaiting = context.user_data.get("awaiting_clarification")
    if awaiting is not None:
        audit_id = awaiting["audit_id"]
        try:
            async with httpx.AsyncClient(timeout=settings.api_llm_timeout_seconds) as client:
                r = await client.post(
                    f"{settings.api_base_url}/internal/clarify",
                    json={"audit_id": audit_id, "value": update.message.text},
                )
        except Exception as e:
            log.exception("clarify call failed")
            await update.message.reply_text(f"Error: {e}\nReply again, or /cancel.")
            return

        if r.status_code in (404, 409):
            # The audit is gone or no longer waiting on us; stop routing here.
            context.user_data.pop("awaiting_clarification", None)
            await update.message.reply_text(
                "That question has expired. Send your log again."
            )
            return
        if r.status_code >= 400:
            await update.message.reply_text(
                f"API error {r.status_code}: {r.text}\nReply again, or /cancel."
            )
            return

        data = r.json()
        if data.get("needs_input"):
            await update.message.reply_text(data["prompt"])
            return
        context.user_data.pop("awaiting_clarification", None)
        await _send_card(
            update,
            data["audit_id"],
            data["preview"],
            data.get("metric_source", "explicit"),
            data.get("draft_sql"),
        )
        return

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
    except Exception as e:
        log.exception("draft call failed")
        await update.message.reply_text(f"Error: {e}")
        return

    if r.status_code == 422:
        await update.message.reply_text(
            r.json().get("detail", "Couldn't understand.")
        )
        return
    if r.status_code >= 400:
        await update.message.reply_text(f"API error {r.status_code}: {r.text}")
        return

    data = r.json()
    if data.get("needs_input") == "unit":
        context.user_data["awaiting_clarification"] = {"audit_id": data["audit_id"]}
        await update.message.reply_text(data["prompt"])
        return
    if data.get("needs_input") == "habit":
        await _send_habit_card(update, data["audit_id"], data["prompt"])
        return
    await _send_card(
        update,
        data["audit_id"],
        data["preview"],
        data.get("metric_source", "explicit"),
        data.get("draft_sql"),
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    if update.effective_user.id != settings.telegram_allowed_user_id:
        await query.answer()
        return

    await query.answer()

    try:
        action, audit_id_str = query.data.split(":", 1)
        audit_id = int(audit_id_str)
    except Exception:
        await query.edit_message_text("Malformed button.")
        return

    if action == "approve":
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{settings.api_base_url}/internal/execute",
                    json={"audit_id": audit_id},
                )
        except Exception as e:
            log.exception("execute call failed")
            await query.edit_message_text(f"Error: {e}")
            return

        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "already_executed":
                await query.edit_message_text(
                    f"✅ Already logged. Audit #{audit_id}"
                )
            else:
                await query.edit_message_text(
                    f"✅ Logged. Audit #{audit_id} (log {data.get('log_id')})"
                )
        else:
            await query.edit_message_text(f"❌ Error {r.status_code}: {r.text}")

    elif action == "feedback":
        context.user_data["awaiting_feedback_for"] = audit_id
        context.user_data["feedback_card_message_id"] = query.message.message_id
        await query.edit_message_text(
            f"Audit #{audit_id} — send feedback as your next message.\n"
            f"(e.g. \"no, 6 miles yesterday\" or \"wrong habit, it was reading\")"
        )

    elif action == "create_habit":
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{settings.api_base_url}/internal/approve_habit",
                    json={"audit_id": audit_id, "accept": True},
                )
        except Exception as e:
            log.exception("approve_habit call failed")
            await query.edit_message_text(f"Error: {e}")
            return

        if r.status_code >= 400:
            await query.edit_message_text(f"❌ {r.status_code}: {r.text}")
            return

        data = r.json()
        if data.get("needs_input") == "unit":
            context.user_data["awaiting_clarification"] = {"audit_id": audit_id}
            await query.edit_message_text(data["prompt"])
            return

        body = _format_card(
            audit_id,
            data["preview"],
            data.get("metric_source", "explicit"),
            data.get("draft_sql"),
        )
        try:
            await query.edit_message_text(
                f"✅ Habit created.\n\n{body}",
                reply_markup=_card_markup(audit_id),
                parse_mode="Markdown",
            )
        except Exception:
            await query.edit_message_text(
                f"✅ Habit created.\n\n📝 {data['preview']}\n\nAudit #{audit_id}",
                reply_markup=_card_markup(audit_id),
            )

    elif action == "cancel_habit":
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                await client.post(
                    f"{settings.api_base_url}/internal/approve_habit",
                    json={"audit_id": audit_id, "accept": False},
                )
        except Exception:
            pass
        await query.edit_message_text(f"Cancelled. Audit #{audit_id}")


async def on_startup(app: Application):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.api_base_url}/health")
            log.info("API health: %s", r.json())
    except Exception as e:
        log.warning("API not reachable on startup: %s", e)


def main():
    token = settings.telegram_bot_token
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("habits", habits_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
