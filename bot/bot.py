"""
HabitFlow Telegram bot: a thin client over the API.

Every message goes to POST /internal/message, which decides what it is:
  log     -> a draft card:      ✅ Save  ✏️ Change  ✖ Cancel  🔍 SQL
  edit    -> a change to a saved log, confirmed first (✅ Apply / 🗑 Delete), with ↩️ Undo
  answer  -> the answer, a small table when there are rows, and 🔍 SQL
  chat    -> help
Two short conversations are tracked in user_data: a unit question (answer
with a button or by typing) and a change to a draft (a quick-fix button or a
typed correction). A question asked in the middle of either is answered
without dropping it.

Sections: config · API · rendering (pure, HTML) · handlers · main.
"""

import html
import logging
import re
from datetime import date

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    telegram_allowed_user_id: int
    api_base_url: str = "http://api:8000"
    # Calls that may use the LLM: a question can take up to 3 SQL attempts + an answer.
    api_llm_timeout_seconds: float = 90


settings = Settings()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("habitflow.bot")

EXAMPLES = "“ran 3 miles” or “read 20 pages yesterday”"
UNREACHABLE = "I can't reach the server right now. Please try again in a moment."
COULDNT_UNDERSTAND = f"Sorry, I couldn't turn that into a log. Try something like {EXAMPLES}."

COMMANDS = [
    ("today", "What you've logged today"),
    ("stats", "Last 30 days and streaks"),
    ("undo", "Undo your last log"),
    ("habits", "Your habits and their units"),
    ("cancel", "Cancel the current question"),
    ("help", "How to use HabitFlow"),
]

# Typed instead of an answer, these mean /cancel.
CANCEL_WORDS = {"cancel", "never mind", "nevermind", "nvm", "stop", "forget it", "skip", "no"}
# A question typed while a unit question or a change is open is answered, and
# the open conversation is kept.
_QUESTION = re.compile(
    r"(\?\s*$)|^(how|what|what's|whats|when|which|why|show|list|compare|summari[sz]e|"
    r"(did|do|have|has|am|was|is|are)\s+(i|my)\b)",
    re.IGNORECASE,
)

TABLE_MAX_ROWS = 15
TABLE_MAX_COLS = 5
TABLE_CELL_WIDTH = 16

# Buttons from cards sent by older versions keep working.
LEGACY_ACTIONS = {
    "approve": "save",
    "feedback": "change",
    "discard": "cancel",
    "create_habit": "create",
    "cancel_habit": "nocreate",
}


def _is_cancel(text: str | None) -> bool:
    return (text or "").strip().lower().rstrip("!.") in CANCEL_WORDS


def _looks_like_question(text: str | None) -> bool:
    """A one-word reply ("km?") is an unsure answer, not a question."""
    text = (text or "").strip()
    return len(text.split()) > 1 and bool(_QUESTION.search(text))


def authorized(update: Update) -> bool:
    return update.effective_user is not None and (
        update.effective_user.id == settings.telegram_allowed_user_id
    )


# ------------------------------------------------------------------
# API
# ------------------------------------------------------------------
async def _post(path: str, payload: dict, timeout: float = 30) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(f"{settings.api_base_url}{path}", json=payload)


async def _get(path: str, params: dict | None = None, timeout: float = 10) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(f"{settings.api_base_url}{path}", params=params)


def _detail(r: httpx.Response):
    try:
        return r.json().get("detail")
    except Exception:
        return None


def _friendly_error(r: httpx.Response) -> str:
    """An API error as something a person can act on. Never raw JSON."""
    detail = _detail(r)
    detail = str(detail) if detail is not None else ""
    log.warning("API %s: %s", r.status_code, detail or r.text)
    if r.status_code == 404:
        return "I can't find that anymore. Send it again."
    if r.status_code == 409:
        if "superseded" in detail:
            return "This draft was replaced by a newer one."
        if "executed" in detail:
            return "That's already saved. ✅"
        if "cancelled" in detail:
            return "This draft was cancelled."
        return "This draft is no longer active. Send your log again."
    if r.status_code == 422:
        return COULDNT_UNDERSTAND
    return "Something went wrong on my side. Please try again in a moment."


# ------------------------------------------------------------------
# Rendering (pure: data in, (HTML text, keyboard) out)
# ------------------------------------------------------------------
def h(value) -> str:
    return html.escape(str(value), quote=False)


_SINGULAR = {  # units are stored plural; mirrors api/app/units.py
    "miles": "mile",
    "meters": "meter",
    "minutes": "minute",
    "hours": "hour",
    "seconds": "second",
    "pages": "page",
    "books": "book",
    "chapters": "chapter",
    "steps": "step",
    "reps": "rep",
    "sets": "set",
    "concepts": "concept",
    "glasses": "glass",
    "liters": "liter",
    "calories": "calorie",
    "laps": "lap",
}


def _qty(amount: float, unit: str) -> str:
    """'1 mile', '2 miles'."""
    amount = float(amount)
    return f"{amount:g} {_SINGULAR.get(unit, unit) if amount == 1 else unit}"


def _keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


def _item_line(item: dict) -> str:
    return f"• <b>{h(item['habit'])}</b> · {h(item['quantity'])} · {h(item['when'])}"


def _items(data: dict) -> list[dict]:
    """Card lines; older API responses only had the preview text."""
    if data.get("items"):
        return data["items"]
    return [
        {"habit": line, "quantity": "", "when": ""}
        for line in (data.get("preview") or "").splitlines()
    ]


def card_markup(audit_id: int, sql_button: bool = True) -> InlineKeyboardMarkup:
    return _keyboard(
        [
            ("✅ Save", f"save:{audit_id}"),
            ("✏️ Change", f"change:{audit_id}"),
            ("✖ Cancel", f"cancel:{audit_id}"),
        ],
        [("🔍 SQL", f"sql:{audit_id}")] if sql_button else [],
    )


def render_card(
    data: dict, header: str = "📝 <b>Log this?</b>"
) -> tuple[str, InlineKeyboardMarkup]:
    items = _items(data)
    if data.get("items"):
        lines = [_item_line(i) for i in items]
    else:
        lines = [f"• {h(i['habit'])}" for i in items]
    text = header + "\n" + "\n".join(lines)
    if data.get("metric_source") == "suggested":
        text += "\n<i>Unit guessed from your habit. Tap ✏️ Change if it's wrong.</i>"
    if data.get("skipped"):
        text += "\n⚠️ Not included: " + h("; ".join(data["skipped"]))
    if data.get("offline"):
        text += "\n⚡ <i>The AI is offline, so the simple parser read this. Please check it.</i>"
    return text, card_markup(data["audit_id"])


def render_unit_question(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    audit_id = data["audit_id"]
    options = data.get("unit_options") or []
    buttons = [(u, f"unit:{audit_id}:{u}") for u in options[:6]]
    rows = [buttons[:3], buttons[3:6], [("✖ Cancel", f"cancel:{audit_id}")]]
    return f"❓ {h(data['prompt'])}\n<i>Tap a unit or type it.</i>", _keyboard(*rows)


def render_habit_question(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    audit_id = data["audit_id"]
    return f"✨ {h(data['prompt'])}", _keyboard(
        [("✨ Create habit", f"create:{audit_id}"), ("✖ No", f"nocreate:{audit_id}")]
    )


def render_draft(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Whatever a DraftResponse asks for: a unit, a new habit, or approval."""
    if data.get("needs_input") == "unit":
        return render_unit_question(data)
    if data.get("needs_input") == "habit":
        return render_habit_question(data)
    return render_card(data)


def render_change_prompt(audit_id: int, card_text: str) -> tuple[str, InlineKeyboardMarkup]:
    lines = [ln for ln in card_text.splitlines() if ln.startswith("•")]
    what = h("\n".join(lines)) if lines else ""
    text = (
        "✏️ <b>What should change?</b>\n"
        + (f"{what}\n" if what else "")
        + "Type the fix, like “6 miles”, “it was yesterday” or “reading, not running”."
    )
    return text, _keyboard(
        [
            ("📅 It was yesterday", f"fix:{audit_id}:yesterday"),
            ("📅 It was today", f"fix:{audit_id}:today"),
        ],
        [("↩️ Keep as is", f"keep:{audit_id}")],
    )


QUICK_FIXES = {"yesterday": "it was yesterday", "today": "it was today"}


def render_saved(data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    summaries = data.get("summaries") or ([data["summary"]] if data.get("summary") else [])
    lines = ["✅ <b>Saved</b>"]
    for s in summaries:
        lines.append(
            f"• <b>{h(s['habit'])}</b> · {h(_qty(s['amount'], s['metric']))} · {h(s['when'])}"
        )
        streak = s.get("streak_days") or 0
        progress = []
        if streak >= 2:
            progress.append(f"🔥 {streak}-day streak")
        elif streak == 1:
            progress.append("🌱 day 1 of a new streak")
        if s.get("week_total"):
            progress.append(f"{_qty(s['week_total'], s['metric'])} this week")
        if progress:
            lines.append("   " + h(" · ".join(progress)))
    log_ids = data.get("log_ids") or []
    if len(log_ids) > 1:
        undo = f"undo_audit:{data['audit_id']}" if data.get("audit_id") else None
    else:
        undo = f"undo:{data['log_id']}" if data.get("log_id") else None
    return "\n".join(lines), (_keyboard([("↩️ Undo", undo)]) if undo else None)


def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{round(value, 2):g}"
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        d = date.fromisoformat(value)
        return f"{d:%a} {d.day} {d:%b}"
    return str(value)


def format_table(columns: list[str], rows: list[list]) -> str:
    """A small monospace table: numbers right-aligned, long cells cut."""
    cols = columns[:TABLE_MAX_COLS]
    shown = [r[:TABLE_MAX_COLS] for r in rows[:TABLE_MAX_ROWS]]
    cells = [[_cell(v) for v in r] for r in shown]
    headers = [c.replace("_", " ") for c in cols]

    def cut(s: str) -> str:
        return s if len(s) <= TABLE_CELL_WIDTH else s[: TABLE_CELL_WIDTH - 1] + "…"

    headers = [cut(x) for x in headers]
    cells = [[cut(x) for x in r] for r in cells]
    widths = [max([len(headers[i])] + [len(r[i]) for r in cells]) for i in range(len(cols))]
    numeric = [
        all(
            isinstance(r[i], (int, float)) and not isinstance(r[i], bool) or r[i] is None
            for r in shown
        )
        and any(r[i] is not None for r in shown)
        for i in range(len(cols))
    ]

    def line(values: list[str]) -> str:
        return "  ".join(
            v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]) for i, v in enumerate(values)
        ).rstrip()

    out = [line(headers), "  ".join("─" * w for w in widths)] + [line(r) for r in cells]
    if len(rows) > TABLE_MAX_ROWS:
        out.append(f"… and {len(rows) - TABLE_MAX_ROWS} more")
    if len(columns) > TABLE_MAX_COLS:
        out.append(f"({len(columns) - TABLE_MAX_COLS} more columns not shown)")
    return "\n".join(out)


def render_answer(data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    icon = "💬" if data.get("ok") else "🤔"
    text = f"{icon} {h(data.get('answer') or 'No answer.')}"
    rows = data.get("rows") or []
    if data.get("ok") and data.get("source") != "template" and len(rows) >= 2:
        text += f"\n<pre>{h(format_table(data.get('columns') or [], rows))}</pre>"
    markup = None
    if data.get("sql") and data.get("query_id"):
        markup = _keyboard([("🔍 SQL", f"qsql:{data['query_id']}")])
    return text, markup


def _when(item: dict) -> str:
    return item["when"].removeprefix("on ")


def _changed(before: dict, after: dict, key: str) -> str:
    """'5 km → <b>6 km</b>', or just '5 km' when that part didn't change."""
    old, new = before[key], after[key]
    return h(old) if old == new else f"{h(old)} → <b>{h(new)}</b>"


def render_edit(data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """A change to a saved log, at any stage: choose, confirm, done, undone."""
    edit_id, status = data.get("edit_id"), data.get("status")
    before, after = data.get("before"), data.get("after")
    delete = data.get("action") == "delete"
    if status == "choosing":
        rows = [
            [(f"{c['habit']} · {c['quantity']} · {_when(c)}", f"epick:{edit_id}:{c['log_id']}")]
            for c in data.get("candidates") or []
        ]
        verb = "delete" if delete else "change"
        return f"🔎 <b>Which one should I {verb}?</b>", _keyboard(
            *rows, [("✖ Cancel", f"ecancel:{edit_id}")]
        )
    if status == "pending" and delete:
        return (
            f"🗑 <b>Delete this log?</b>\n• <b>{h(before['habit'])}</b> · "
            f"{h(before['quantity'])} · {h(before['when'])}",
            _keyboard([("🗑 Delete", f"eapply:{edit_id}"), ("✖ Keep it", f"ecancel:{edit_id}")]),
        )
    if status == "pending":
        return (
            f"✏️ <b>Change this log?</b>\n• <b>{h(before['habit'])}</b> · "
            f"{_changed(before, after, 'quantity')} · {_changed(before, after, 'when')}",
            _keyboard([("✅ Apply", f"eapply:{edit_id}"), ("✖ Cancel", f"ecancel:{edit_id}")]),
        )
    if status == "applied":
        undo = _keyboard([("↩️ Undo", f"erevert:{edit_id}")])
        if delete:
            return (
                f"🗑 <b>Deleted</b>: {h(before['quantity'])} of {h(before['habit'])} "
                f"{h(before['when'])}",
                undo,
            )
        return (
            f"✅ <b>Changed</b>\n• <b>{h(after['habit'])}</b> · {h(after['quantity'])} · "
            f"{h(after['when'])}\n<i>was {h(before['quantity'])} · {h(before['when'])}</i>",
            undo,
        )
    if status == "reverted":
        return (
            f"↩️ <b>Put back</b>: {h(before['quantity'])} of {h(before['habit'])} "
            f"{h(before['when'])}",
            None,
        )
    if status == "cancelled":
        return "✖ OK, I left it as it was.", None
    return f"🤔 {h(data.get('message') or 'I could not work out which log to change.')}", None


def render_sql(title: str, sql: str) -> str:
    return f"🔍 <b>{h(title)}</b>\n<pre>{h(sql.strip())}</pre>"


HELP_TEXT = (
    "<b>Log</b> what you did, in plain words:\n"
    "  “ran 4 miles” · “read 20 pages yesterday”\n"
    "  “meditated 10 min and 30 pushups” (several at once)\n"
    "  “learned rust for 2 hours” (new habits are created for you)\n"
    "You'll get a card: ✅ Save · ✏️ Change · ✖ Cancel. Nothing is saved until you tap ✅, "
    "and ↩️ Undo is there afterwards.\n\n"
    "<b>Ask</b> about your routine:\n"
    "  “how much did I read this month?”\n"
    "  “what's my reading pattern?”\n"
    "  “how many hours did I work last week?”\n"
    "  “which days did I skip meditation?”\n"
    "Tap 🔍 SQL to see exactly how it was counted.\n\n"
    "<b>Fix</b> a saved log:\n"
    "  “change yesterday's run to 6 km”\n"
    "  “move today's reading to yesterday”\n"
    "  “delete Monday's meditation”\n"
    "You confirm first, and can undo it.\n\n"
    "<b>Commands</b>\n" + "\n".join(f"  /{name} — {h(desc)}" for name, desc in COMMANDS)
)


# ------------------------------------------------------------------
# Sending
# ------------------------------------------------------------------
def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


async def _reply(message, text: str, markup=None):
    """Reply in HTML; if Telegram rejects the markup, send it as plain text."""
    try:
        return await message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        log.warning("HTML reply failed; sending plain text")
        return await message.reply_text(_plain(text), reply_markup=markup)


async def _edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        log.warning("HTML edit failed; editing as plain text")
        try:
            await query.edit_message_text(_plain(text), reply_markup=markup)
        except Exception:
            log.warning("could not edit message")


async def _typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show "typing…" while the API works. Cosmetic: never let it fail a request."""
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
    except Exception:
        pass


async def _clear_buttons(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id):
    """Remove the buttons from an earlier prompt that no longer applies."""
    if not message_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        pass


# ------------------------------------------------------------------
# Conversation state
# ------------------------------------------------------------------
UNIT = "awaiting_unit"  # {"audit_id", "message_id"}
EDIT = "editing"  # {"audit_id", "card_message_id", "prompt_message_id"}


async def _drop_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int, key: str):
    state = context.user_data.pop(key, None)
    if state:
        await _clear_buttons(
            context, chat_id, state.get("message_id") or state.get("prompt_message_id")
        )
    return state


async def _show_draft(message, context: ContextTypes.DEFAULT_TYPE, data: dict):
    """Send a DraftResponse and remember a unit question if it is one."""
    text, markup = render_draft(data)
    sent = await _reply(message, text, markup)
    if data.get("needs_input") == "unit":
        context.user_data[UNIT] = {
            "audit_id": data["audit_id"],
            "message_id": getattr(sent, "message_id", None),
        }


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await _reply(
        update.message,
        "👋 <b>HabitFlow is ready.</b>\n\n"
        f"Tell me what you did, like {h(EXAMPLES)}, and tap ✅ to save it.\n"
        "Or ask things like “how much did I read this month?”.\n\n"
        "/help shows everything.",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await _reply(update.message, HELP_TEXT)


async def _simple_get(update: Update, path: str, params: dict | None = None):
    try:
        r = await _get(path, params)
    except Exception:
        log.exception("%s call failed", path)
        await update.message.reply_text(UNREACHABLE)
        return None
    if r.status_code != 200:
        await update.message.reply_text(_friendly_error(r))
        return None
    return r.json()


async def habits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    habits = await _simple_get(update, "/internal/habits")
    if habits is None:
        return
    if not habits:
        await update.message.reply_text(
            f"No habits yet. Log something like {EXAMPLES} and I'll create it."
        )
        return
    lines = [
        f"• <b>{h(x['display_name'])}</b> · {h(x.get('metric') or 'no default unit')}"
        for x in habits
    ]
    await _reply(update.message, "📋 <b>Your habits</b>\n" + "\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    rows = await _simple_get(update, "/internal/stats", {"chat_id": update.effective_chat.id})
    if rows is None:
        return
    if not rows:
        await update.message.reply_text("No logs in the last 30 days. Send one to get started!")
        return
    lines = ["📊 <b>Last 30 days</b>"]
    for row in rows:
        days = row["days"]
        line = (
            f"• <b>{h(row['habit'])}</b> · {h(_qty(row['total'], row['metric']))} "
            f"on {days} day{'s' if days != 1 else ''}"
        )
        if row.get("streak_days", 0) >= 2:
            line += f" · 🔥 {row['streak_days']}"
        lines.append(line)
    await _reply(update.message, "\n".join(lines))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    data = await _simple_get(update, "/internal/today")
    if data is None:
        return
    logs = data["logs"]
    if not logs:
        await update.message.reply_text(
            f"Nothing logged yet today. Send something like {EXAMPLES}."
        )
        return
    totals: dict[tuple[str, str], float] = {}
    for item in logs:
        key = (item["habit"], item["metric"])
        totals[key] = totals.get(key, 0) + item["amount"]
    lines = ["📅 <b>Today so far</b>"] + [
        f"• <b>{h(habit)}</b> · {h(_qty(amount, metric))}"
        for (habit, metric), amount in totals.items()
    ]
    await _reply(update.message, "\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    try:
        r = await _post("/internal/undo", {})
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
    await _reply(update.message, f"↩️ <b>Undone</b>: {h(r.json()['preview'])}")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    editing = await _drop_state(context, chat_id, EDIT)
    unit = await _drop_state(context, chat_id, UNIT)
    if unit:
        await _discard_quietly(unit["audit_id"])
    if editing or unit:
        await update.message.reply_text("OK, cancelled.")
    else:
        await update.message.reply_text("There's nothing to cancel.")


async def _discard_quietly(audit_id: int):
    try:
        await _post("/internal/discard", {"audit_id": audit_id})
    except Exception:
        log.exception("discard failed")


# ------------------------------------------------------------------
# Text messages
# ------------------------------------------------------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    text = update.message.text or ""
    chat_id = update.effective_chat.id

    editing = context.user_data.get(EDIT)
    if editing and not _looks_like_question(text):
        if _is_cancel(text):
            await _drop_state(context, chat_id, EDIT)
            await update.message.reply_text("OK, I left the draft as it was.")
            return
        await _send_feedback(update.message, context, chat_id, editing["audit_id"], text)
        return

    unit = context.user_data.get(UNIT)
    if unit and not _looks_like_question(text):
        if _is_cancel(text):
            await _drop_state(context, chat_id, UNIT)
            await _discard_quietly(unit["audit_id"])
            await update.message.reply_text("OK, dropped it.")
            return
        await _typing(update, context)
        outcome = await _send_unit(
            update.message, context, chat_id, unit["audit_id"], text, typed=True
        )
        if outcome == "new_log":
            await _send_message(update, context)
        return

    await _typing(update, context)
    await _send_message(update, context)
    if editing or unit:
        what = "your change" if editing else "the unit"
        await update.message.reply_text(f"(I'm still waiting for {what} above, or send /cancel.)")


async def _send_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None
):
    """POST /internal/message and show what comes back."""
    try:
        r = await _post(
            "/internal/message",
            {
                "chat_id": update.effective_chat.id,
                "text": text or update.message.text,
                "message_id": update.message.message_id,
            },
            timeout=settings.api_llm_timeout_seconds,
        )
    except Exception:
        log.exception("message call failed")
        await update.message.reply_text(UNREACHABLE)
        return
    if r.status_code >= 400:
        await update.message.reply_text(_friendly_error(r))
        return

    data = r.json()
    kind = data.get("kind")
    if kind == "log":
        if context.user_data.get(UNIT) and data["draft"].get("needs_input") != "unit":
            await _drop_state(context, update.effective_chat.id, UNIT)
        await _show_draft(update.message, context, data["draft"])
    elif kind == "answer":
        text, markup = render_answer(data["answer"])
        await _reply(update.message, text, markup)
    elif kind == "edit":
        text, markup = render_edit(data["edit"])
        await _reply(update.message, text, markup)
    elif kind == "chat":
        await _reply(update.message, HELP_TEXT)
    else:
        await update.message.reply_text(data.get("text") or COULDNT_UNDERSTAND)


async def _send_unit(
    message, context, chat_id: int, audit_id: int, value: str, typed: bool, query=None
):
    """Answer a unit question (typed, or a unit button when query is set)."""
    try:
        r = await _post(
            "/internal/clarify",
            {"audit_id": audit_id, "value": value},
            timeout=settings.api_llm_timeout_seconds,
        )
    except Exception:
        log.exception("clarify call failed")
        await message.reply_text(f"{UNREACHABLE} Try again, or /cancel.")
        return

    detail = _detail(r)
    if r.status_code == 409 and isinstance(detail, dict) and detail.get("code") == "new_log":
        # Not a unit but a new log ("read 20 pages"): drop the question, log this instead.
        await _drop_state(context, chat_id, UNIT)
        await _reply(
            message,
            f"OK, I dropped “{h(detail.get('dropped', 'the earlier one'))}” "
            "(no unit) and read this as a new log.",
        )
        return "new_log"
    if r.status_code in (404, 409):
        await _drop_state(context, chat_id, UNIT)
        await message.reply_text("That question has expired. Send your log again.")
        return
    if r.status_code >= 400:
        await message.reply_text(f"{_friendly_error(r)} Try again, or /cancel.")
        return

    data = r.json()
    if data.get("needs_input") == "unit":
        text, markup = render_unit_question(data)
        if query:
            await _edit(query, text, markup)
        else:
            await _drop_state(context, chat_id, UNIT)
            await _show_draft(message, context, data)
        return
    if typed:
        await _drop_state(context, chat_id, UNIT)  # the question's buttons no longer apply
    else:
        context.user_data.pop(UNIT, None)  # the question message itself becomes the card
    text, markup = render_card(data)
    if query:
        await _edit(query, text, markup)
    else:
        await _reply(message, text, markup)


async def _send_feedback(message, context, chat_id: int, audit_id: int, feedback: str):
    """Apply a correction; the old card is retired and the new one sent below."""
    editing = context.user_data.get(EDIT) or {}
    try:
        r = await _post(
            "/internal/feedback",
            {"audit_id": audit_id, "feedback": feedback},
            timeout=settings.api_llm_timeout_seconds,
        )
    except Exception:
        log.exception("feedback call failed")
        await message.reply_text(f"{UNREACHABLE} Send your fix again, or “cancel”.")
        return  # the change stays open: resending retries it
    if r.status_code == 422:
        await message.reply_text(
            "I couldn't apply that change. Try rephrasing, like “6 miles, not 4”, or /cancel."
        )
        return
    await _drop_state(context, chat_id, EDIT)
    if r.status_code >= 400:
        await message.reply_text(_friendly_error(r))
        return

    card_id = editing.get("card_message_id")
    if card_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=card_id,
                text=f"✏️ Changed: “{feedback}”. See the updated draft below.",
            )
        except Exception:
            log.warning("could not retire card %s", card_id)
    data = r.json()
    if data.get("needs_input"):
        await _show_draft(message, context, data)
        return
    text, markup = render_card(data, header="🔄 <b>Updated. Log this?</b>")
    await _reply(message, text, markup)


# ------------------------------------------------------------------
# Buttons
# ------------------------------------------------------------------
def _parse_callback(data: str) -> tuple[str, int, str | None]:
    parts = (data or "").split(":", 2)
    if len(parts) < 2:
        raise ValueError(data)
    action = LEGACY_ACTIONS.get(parts[0], parts[0])
    return action, int(parts[1]), parts[2] if len(parts) == 3 else None


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    if update.effective_user.id != settings.telegram_allowed_user_id:
        await query.answer()
        return
    try:
        action, target_id, extra = _parse_callback(query.data)
    except Exception:
        await query.answer()
        await query.edit_message_text("That button is no longer valid.")
        return

    handler = BUTTONS.get(action)
    if handler is None:
        await query.answer()
        await query.edit_message_text("That button is no longer valid.")
        return
    await handler(update, context, query, target_id, extra)


async def _api_or_popup(query, path: str, payload: dict, timeout: float = 30):
    """POST and return the JSON, or show the problem in a popup and return None."""
    try:
        r = await _post(path, payload, timeout=timeout)
    except Exception:
        log.exception("%s failed", path)
        await query.answer(UNREACHABLE, show_alert=True)
        return None
    if r.status_code >= 400:
        await query.answer(_friendly_error(r), show_alert=True)  # leave the message alone
        return None
    await query.answer()
    return r.json()


async def _close_edit_for(context, chat_id: int, audit_id: int):
    editing = context.user_data.get(EDIT)
    if editing and editing["audit_id"] == audit_id:
        await _drop_state(context, chat_id, EDIT)


async def on_save(update, context, query, audit_id, extra):
    await _close_edit_for(context, update.effective_chat.id, audit_id)
    data = await _api_or_popup(query, "/internal/execute", {"audit_id": audit_id})
    if data is None:
        return
    if data.get("status") == "already_executed":
        await _edit(query, "✅ Already saved.")
        return
    text, markup = render_saved({**data, "audit_id": audit_id})
    await _edit(query, text, markup)


async def on_cancel(update, context, query, audit_id, extra):
    chat_id = update.effective_chat.id
    await _close_edit_for(context, chat_id, audit_id)
    unit = context.user_data.get(UNIT)
    if unit and unit["audit_id"] == audit_id:
        context.user_data.pop(UNIT, None)
    data = await _api_or_popup(query, "/internal/discard", {"audit_id": audit_id})
    if data is not None:
        await _edit(query, "✖ Cancelled. Nothing was saved.")


async def on_change(update, context, query, audit_id, extra):
    await query.answer()
    chat_id = update.effective_chat.id
    await _drop_state(context, chat_id, EDIT)  # one change at a time
    text, markup = render_change_prompt(audit_id, getattr(query.message, "text", "") or "")
    sent = await _reply(query.message, text, markup)
    context.user_data[EDIT] = {
        "audit_id": audit_id,
        "card_message_id": query.message.message_id,
        "prompt_message_id": getattr(sent, "message_id", None),
    }


async def on_fix(update, context, query, audit_id, extra):
    feedback = QUICK_FIXES.get(extra or "")
    if feedback is None:
        await query.answer()
        return
    await query.answer()
    editing = context.user_data.get(EDIT) or {}
    if editing.get("audit_id") != audit_id:  # an old prompt: still apply it
        context.user_data[EDIT] = {
            "audit_id": audit_id,
            "card_message_id": None,
            "prompt_message_id": query.message.message_id,
        }
    await _edit(query, f"✏️ {h(feedback.capitalize())}…")
    context.user_data[EDIT]["prompt_message_id"] = None  # already edited
    await _send_feedback(query.message, context, update.effective_chat.id, audit_id, feedback)


async def on_keep(update, context, query, audit_id, extra):
    await query.answer()
    await _close_edit_for(context, update.effective_chat.id, audit_id)
    await _edit(query, "OK, I left the draft as it was.")


async def on_unit(update, context, query, audit_id, extra):
    if not extra:
        await query.answer()
        return
    await query.answer()
    await _send_unit(
        query.message, context, update.effective_chat.id, audit_id, extra, typed=False, query=query
    )


async def on_create(update, context, query, audit_id, extra):
    data = await _api_or_popup(
        query, "/internal/approve_habit", {"audit_id": audit_id, "accept": True}
    )
    if data is None:
        return
    if data.get("needs_input") == "unit":
        text, markup = render_unit_question(data)
        await _edit(query, text, markup)
        context.user_data[UNIT] = {"audit_id": audit_id, "message_id": query.message.message_id}
        return
    text, markup = render_card(data, header="✨ <b>New habit created. Log this?</b>")
    await _edit(query, text, markup)


async def on_nocreate(update, context, query, audit_id, extra):
    data = await _api_or_popup(
        query, "/internal/approve_habit", {"audit_id": audit_id, "accept": False}
    )
    if data is not None:
        await _edit(query, "OK, I didn't create it.")


async def _undo(query, payload: dict):
    data = await _api_or_popup(query, "/internal/undo", payload)
    if data is None:
        return
    word = "Already undone" if data.get("status") == "already_undone" else "Undone"
    await _edit(query, f"↩️ <b>{word}</b>: {h(data['preview'])}")


async def on_undo(update, context, query, log_id, extra):
    await _undo(query, {"log_id": log_id})


async def on_undo_audit(update, context, query, audit_id, extra):
    await _undo(query, {"audit_id": audit_id})


async def on_draft_sql(update, context, query, audit_id, extra):
    try:
        r = await _get(f"/internal/drafts/{audit_id}")
    except Exception:
        await query.answer(UNREACHABLE, show_alert=True)
        return
    if r.status_code != 200:
        await query.answer(_friendly_error(r), show_alert=True)
        return
    data = r.json()
    sql = data.get("final_sql") or data.get("draft_sql")
    if not sql:
        await query.answer("No SQL for this one.", show_alert=True)
        return
    await query.answer()
    await _reply(query.message, render_sql("SQL for this log", sql))
    if data.get("status") == "pending":
        try:
            await query.edit_message_reply_markup(
                reply_markup=card_markup(audit_id, sql_button=False)
            )
        except Exception:
            pass


async def on_query_sql(update, context, query, query_id, extra):
    try:
        r = await _get(f"/internal/queries/{query_id}")
    except Exception:
        await query.answer(UNREACHABLE, show_alert=True)
        return
    if r.status_code != 200 or not r.json().get("sql"):
        await query.answer("I don't have the SQL for that anymore.", show_alert=True)
        return
    await query.answer()
    data = r.json()
    title = "SQL used" + (" (built-in template)" if data.get("source") == "template" else "")
    await _reply(query.message, render_sql(title, data["sql"]))
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _edit_step(query, edit_id: int, step: str, payload: dict | None = None):
    """One step of a change to a saved log. The API's refusals are already
    written for people ("A newer change replaced this one."), so show them."""
    try:
        r = await _post(f"/internal/edits/{edit_id}/{step}", payload or {})
    except Exception:
        log.exception("edit %s failed", step)
        await query.answer(UNREACHABLE, show_alert=True)
        return
    if r.status_code >= 400:
        detail = _detail(r)
        await query.answer(detail if isinstance(detail, str) else _friendly_error(r),
                           show_alert=True)
        return
    await query.answer()
    text, markup = render_edit(r.json())
    await _edit(query, text, markup)


async def on_edit_apply(update, context, query, edit_id, extra):
    await _edit_step(query, edit_id, "apply")


async def on_edit_cancel(update, context, query, edit_id, extra):
    await _edit_step(query, edit_id, "cancel")


async def on_edit_pick(update, context, query, edit_id, extra):
    if not (extra or "").isdigit():
        await query.answer()
        await query.edit_message_text("That button is no longer valid.")
        return
    await _edit_step(query, edit_id, "choose", {"log_id": int(extra)})


async def on_edit_revert(update, context, query, edit_id, extra):
    await _edit_step(query, edit_id, "undo")


BUTTONS = {
    "save": on_save,
    "cancel": on_cancel,
    "change": on_change,
    "fix": on_fix,
    "keep": on_keep,
    "unit": on_unit,
    "create": on_create,
    "nocreate": on_nocreate,
    "undo": on_undo,
    "undo_audit": on_undo_audit,
    "sql": on_draft_sql,
    "qsql": on_query_sql,
    "eapply": on_edit_apply,
    "ecancel": on_edit_cancel,
    "epick": on_edit_pick,
    "erevert": on_edit_revert,
}


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def on_startup(app: Application):
    try:
        await app.bot.set_my_commands([BotCommand(n, d) for n, d in COMMANDS])
    except Exception as e:
        log.warning("could not set command menu: %s", e)


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
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
