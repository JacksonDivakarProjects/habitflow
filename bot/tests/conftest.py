"""
Bot handler tests. Telegram objects are stand-ins exposing only what the
handlers touch; the API is an httpx.MockTransport. No network calls.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

ALLOWED_USER = 42
CHAT = 4242

# bot.settings reads these at import time.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("TELEGRAM_ALLOWED_USER_ID", str(ALLOWED_USER))

_RealAsyncClient = httpx.AsyncClient


class FakeAPI:
    def __init__(self):
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.requests: list[httpx.Request] = []
        self.timeouts: dict[str, object] = {}

    def on(self, method: str, path: str, status: int = 200, json=None, raises=None):
        self.routes[(method, path)] = (status, raises or json)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            raise AssertionError(f"unexpected API call: {key}")
        status, body = self.routes[key]
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    def client(self, timeout=None, **kwargs):
        client = _RealAsyncClient(
            transport=httpx.MockTransport(self._handle), timeout=timeout, **kwargs
        )
        original_request = client.request

        async def request(method, url, **kw):
            self.timeouts[httpx.URL(str(url)).path] = timeout
            return await original_request(method, url, **kw)

        client.request = request
        return client

    def last_json(self):
        import json

        return json.loads(self.requests[-1].content)


@pytest.fixture
def api(monkeypatch):
    import bot

    fake = FakeAPI()
    monkeypatch.setattr(bot.httpx, "AsyncClient", fake.client)
    return fake


_next_message_id = iter(range(500, 10_000))


def _sent_message(*args, **kwargs):
    return SimpleNamespace(message_id=next(_next_message_id))


def make_update(text=None, user_id=ALLOWED_USER, message_id=10, callback_data=None,
                message_text="📝 Log this?\n• Running · 4 miles · today"):
    message = SimpleNamespace(
        text=text, message_id=message_id, reply_text=AsyncMock(side_effect=_sent_message)
    )
    query = None
    if callback_data is not None:
        query = SimpleNamespace(
            data=callback_data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            message=SimpleNamespace(
                message_id=message_id, text=message_text,
                reply_text=AsyncMock(side_effect=_sent_message),
            ),
        )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=CHAT),
        message=message,
        callback_query=query,
    )


def make_context(**user_data):
    return SimpleNamespace(
        user_data=dict(user_data),
        args=[],
        bot=SimpleNamespace(
            edit_message_text=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            send_chat_action=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


def replies(update) -> list[str]:
    """Texts sent as replies to the user's message."""
    return [c.args[0] for c in update.message.reply_text.await_args_list]


def query_replies(update) -> list[str]:
    """Texts sent as replies from a button's message."""
    return [c.args[0] for c in update.callback_query.message.reply_text.await_args_list]


def markup_of(mock) -> object:
    return mock.await_args.kwargs.get("reply_markup")


def buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in (markup.inline_keyboard if markup else []) for b in row]


def edited(update) -> str:
    return update.callback_query.edit_message_text.await_args.args[0]


def popup(update) -> str | None:
    call = update.callback_query.answer.await_args
    return call.args[0] if call.args and call.kwargs.get("show_alert") else None
