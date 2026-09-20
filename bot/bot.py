import logging

import httpx
from pydantic_settings import BaseSettings
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


class Settings(BaseSettings):
    telegram_bot_token: str
    telegram_allowed_user_id: int
    api_base_url: str = "http://api:8000"

    class Config:
        env_file = ".env"
        extra = "ignore"


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
        "Commands: /help /habits /stats"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(
        "Send natural language like:\n"
        '  "ran 4 miles today"\n'
        '  "read 20 pages"\n'
        '  "meditated 10 minutes"\n\n'
        "You'll get a verification card before anything is written.\n\n"
        "Commands:\n"
        "  /habits — list tracked habits\n"
        "  /stats  — streaks and totals"
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
    await update.message.reply_text("Stats coming in Phase 4.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    text = update.message.text
    chat_id = update.effective_chat.id
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.api_base_url}/internal/draft",
                json={"chat_id": chat_id, "text": text},
            )
        if r.status_code == 501:
            await update.message.reply_text(
                "Loop 1 not wired yet. The foundation is up; the LLM lands in Phase 5."
            )
        elif r.status_code >= 400:
            await update.message.reply_text(f"API error {r.status_code}: {r.text}")
        else:
            data = r.json()
            await update.message.reply_text(
                f"Draft ready (audit {data['audit_id']}):\n{data.get('preview', '')}"
            )
    except Exception as e:
        log.exception("draft call failed")
        await update.message.reply_text(f"Error: {e}")


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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()