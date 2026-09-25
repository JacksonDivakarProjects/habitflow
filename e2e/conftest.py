"""
End-to-end harness: the real bot handlers talk to the real API app (in
process, via httpx.ASGITransport) and a real Postgres. Only Telegram and the
LLM are simulated. `Chat` plays the user: say() sends text or a command,
tap() presses a button on a message, and every bot message and edit is kept.
"""

import asyncio
import html
import importlib.util
import itertools
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "api"), str(ROOT / "bot")]

ALLOWED_USER = 777
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:e2e")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", str(ALLOWED_USER))

# Reuse the API suite's database setup and fakes (loaded by path: both
# services have a `tests` package, so a normal import would be ambiguous).
_spec = importlib.util.spec_from_file_location("api_harness", ROOT / "api/tests/conftest.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

pytest_configure = harness.pytest_configure
migrated, clean_db, db = harness.migrated, harness.clean_db, harness.db
fake_llm, make_intent = harness.fake_llm, harness.make_intent
llm_http = harness.llm_http  # autouse: /classify, /query_sql, /answer are scripted or offline


def _from_html(text: str) -> str:
    """What Telegram hands back as message.text after rendering HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _rendered(text: str, parse_mode) -> str:
    if parse_mode == "HTML":
        return _from_html(text)
    if parse_mode == "Markdown":
        return _plain(text)
    return text


def _plain(text: str) -> str:
    """What Telegram hands back as message.text after rendering Markdown."""
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"\\([_*`\[\]])", "\x00\\1", text)  # keep escaped chars
    text = re.sub(r"[_*`]", "", text)
    return text.replace("\x00", "")


@dataclass
class Message:
    id: int
    text: str
    markup: object = None
    from_user: bool = False
    edits: list[str] = field(default_factory=list)

    @property
    def message_id(self) -> int:  # as on a real telegram.Message
        return self.id

    @property
    def buttons(self) -> list[str]:
        if not self.markup:
            return []
        return [b.text for row in self.markup.inline_keyboard for b in row]


class Chat:
    def __init__(self, bot_module, user_id=ALLOWED_USER):
        self.bot = bot_module
        self.user_id = user_id
        self.messages: list[Message] = []
        self.popups: list[str] = []
        self.deleted: list[Message] = []
        self._ids = itertools.count(1)
        self.context = SimpleNamespace(
            user_data={},
            args=[],
            bot=SimpleNamespace(
                edit_message_text=self._edit_by_id,
                edit_message_reply_markup=self._edit_markup_by_id,
                send_chat_action=AsyncMock(),
                send_message=self._send_message,
            ),
        )

    # --- what the bot calls -------------------------------------------
    def _add(self, text, markup=None, parse_mode=None, from_user=False) -> Message:
        msg = Message(next(self._ids), _rendered(text, parse_mode), markup, from_user)
        self.messages.append(msg)
        return msg

    async def _reply(self, text, reply_markup=None, parse_mode=None):
        return self._add(text, reply_markup, parse_mode)

    async def _send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        return self._add(text, reply_markup, parse_mode)

    def _edit(self, msg: Message, text, reply_markup=None, parse_mode=None):
        msg.edits.append(msg.text)
        msg.text = _rendered(text, parse_mode)
        msg.markup = reply_markup

    async def _edit_by_id(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        self._edit(self._by_id(message_id), text, reply_markup, parse_mode)

    async def _edit_markup_by_id(self, chat_id, message_id, reply_markup=None):
        self._by_id(message_id).markup = reply_markup

    def _by_id(self, message_id) -> Message:
        return next(m for m in self.messages if m.id == message_id)

    # --- what the user does -------------------------------------------
    def say(self, text: str) -> list[str]:
        """Send a message or /command; return the texts of new bot messages."""
        before = len(self.messages)
        user_msg = self._add(text, from_user=True)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_chat=SimpleNamespace(id=self.user_id),
            message=SimpleNamespace(text=text, message_id=user_msg.id, reply_text=self._reply),
            callback_query=None,
        )
        if text.startswith("/"):
            name, *args = text[1:].split()
            self.context.args = args
            handler = {n: getattr(self.bot, f"{n}_cmd") for n, _ in self.bot.COMMANDS}
            handler["start"] = self.bot.start
            handler["help"] = self.bot.help_cmd
            asyncio.run(handler[name](update, self.context))
        else:
            asyncio.run(self.bot.handle_text(update, self.context))
        return [m.text for m in self.messages[before:] if not m.from_user]

    def texts_since(self, index: int) -> list[str]:
        """Texts of the bot messages sent after self.messages[index]."""
        return [m.text for m in self.messages[index:] if not m.from_user]

    def tap(self, label: str, message: Message | None = None) -> Message:
        """Press the button labelled `label` (on the latest message that has it)."""
        if message is None:
            message = next(
                (m for m in reversed(self.messages) if label in m.buttons), None
            )
            assert message, f"no message has a {label!r} button; last: {self.last.text!r}"
        button = next(
            b for row in message.markup.inline_keyboard for b in row if b.text == label
        )
        popups = self.popups

        async def answer(text=None, show_alert=False):
            if show_alert:
                popups.append(text)

        async def edit(text, reply_markup=None, parse_mode=None):
            self._edit(message, text, reply_markup, parse_mode)

        async def edit_markup(reply_markup=None):
            message.markup = reply_markup

        async def delete():
            self.messages.remove(message)
            self.deleted.append(message)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_chat=SimpleNamespace(id=self.user_id),
            message=None,
            callback_query=SimpleNamespace(
                data=button.callback_data,
                answer=answer,
                edit_message_text=edit,
                edit_message_reply_markup=edit_markup,
                message=SimpleNamespace(message_id=message.id, text=message.text,
                                        reply_text=self._reply, delete=delete),
            ),
        )
        asyncio.run(self.bot.button_callback(update, self.context))
        return message

    @property
    def last(self) -> Message:
        return next(m for m in reversed(self.messages) if not m.from_user)


@pytest.fixture
def bot_module(monkeypatch):
    import bot
    from app.main import app

    real_client = httpx.AsyncClient

    def client(timeout=None, **kwargs):
        return real_client(transport=httpx.ASGITransport(app=app), timeout=timeout, **kwargs)

    monkeypatch.setattr(bot.httpx, "AsyncClient", client)
    return bot


@pytest.fixture
def chat(bot_module):
    return Chat(bot_module)
