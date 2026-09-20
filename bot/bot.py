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
        '  "read 20 pages"\n'
        '  "meditated 10 minutes"\n\n'
        "If you forget the unit, I'll ask.\n"
        "You'll get a verification card before anything is written.\n\n"
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
        if r.status_code == 200:
            habits = r.json()
            if not habits:
                await update.message.reply_text("No habits yet.")
                return
            lines = [f"• {h['display_name']} ({h['metric']})" for h in habits]
            await update.message.reply_text("Tracked habits:\n" + "\n".join(lines))
        else:
            await update.message.reply_text(f"API error {r.status_code}")
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
    await update.message.reply_text("Cancelled.")


async def _send_card(update: Update, audit_id: int, preview: str):
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{audit_id}"),
                InlineKeyboardButton("✏️ Feedback", callback_data=f"feedback:{audit_id}"),
            ]
        ]
    )
    await update.message.reply_text(
        f"📝 {preview}\n\nAudit #{audit_id}",
        reply_markup=keyboard,
    )


async def _send_feedback(update: Update, audit_id: int, feedback: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/feedback",
                json={"audit_id": audit_id, "feedback": feedback},
            )
        if r.status_code == 501:
            await update.message.reply_text(
                "Feedback loop implemented in Phase 6. "
                "For now, send a fresh message to draft again."
            )
        elif r.status_code >= 400:
            await update.message.reply_text(f"API error {r.status_code}: {r.text}")
        else:
            await update.message.reply_text("Updated draft.")
    except Exception as e:
        log.exception("feedback call failed")
        await update.message.reply_text(f"Error: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    # 1. Awaiting feedback (Phase 6 stub)
    pending_feedback = context.user_data.pop("awaiting_feedback_for", None)
    if pending_feedback is not None:
        await _send_feedback(update, pending_feedback, update.message.text)
        return

    # 2. Awaiting clarification (missing unit, etc.)
    awaiting = context.user_data.get("awaiting_clarification")
    if awaiting is not None:
        audit_id = awaiting["audit_id"]
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"{settings.api_base_url}/internal/clarify",
                    json={"audit_id": audit_id, "value": update.message.text},
                )
        except Exception as e:
            log.exception("clarify call failed")
            await update.message.reply_text(f"Error: {e}")
            return

        if r.status_code >= 400:
            await update.message.reply_text(f"API error {r.status_code}: {r.text}")
            return

        data = r.json()
        if data.get("needs_input"):
            await update.message.reply_text(data["prompt"])
            return

        context.user_data.pop("awaiting_clarification", None)
        await _send_card(update, data["audit_id"], data["preview"])
        return

    # 3. Fresh message → draft
    text = update.message.text
    chat_id = update.effective_chat.id

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/draft",
                json={"chat_id": chat_id, "text": text},
            )
    except Exception as e:
        log.exception("draft call failed")
        await update.message.reply_text(f"Error: {e}")
        return

    if r.status_code == 422:
        detail = r.json().get("detail", "Couldn't understand that.")
        await update.message.reply_text(detail)
        return

    if r.status_code >= 400:
        await update.message.reply_text(f"API error {r.status_code}: {r.text}")
        return

    data = r.json()

    if data.get("needs_input"):
        context.user_data["awaiting_clarification"] = {
            "audit_id": data["audit_id"],
        }
        await update.message.reply_text(data["prompt"])
        return

    await _send_card(update, data["audit_id"], data["preview"])


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
                await query.edit_message_text(f"✅ Already logged. Audit #{audit_id}")
            else:
                await query.edit_message_text(
                    f"✅ Logged. Audit #{audit_id} (log {data.get('log_id')})"
                )
        else:
            await query.edit_message_text(f"❌ Error {r.status_code}: {r.text}")

    elif action == "feedback":
        context.user_data["awaiting_feedback_for"] = audit_id
        await query.edit_message_text(
            f"Audit #{audit_id} — send feedback as your next message."
        )


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