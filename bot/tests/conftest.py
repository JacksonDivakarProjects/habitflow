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


def make_update(text=None, user_id=ALLOWED_USER, message_id=10, callback_data=None):
    message = SimpleNamespace(text=text, message_id=message_id, reply_text=AsyncMock())
    query = None
    if callback_data is not None:
        query = SimpleNamespace(
            data=callback_data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(message_id=message_id, text="📝 4 miles of Running today"),
        )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=CHAT),
        message=message,
        callback_query=query,
    )


class FakeJob:
    def __init__(self, name, **kwargs):
        self.name, self.kwargs, self.removed = name, kwargs, False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    """The slice of telegram.ext.JobQueue the bot uses."""

    def __init__(self):
        self.jobs: list[FakeJob] = []

    def run_daily(self, callback, time, chat_id, name):
        job = FakeJob(name, callback=callback, time=time, chat_id=chat_id)
        self.jobs.append(job)
        return job

    def run_once(self, callback, when, name=None):
        job = FakeJob(name, callback=callback, when=when)
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def active(self):
        return [j for j in self.jobs if not j.removed]


def make_context(args=None, job_queue=None, **user_data):
    return SimpleNamespace(
        user_data=dict(user_data),
        args=list(args or []),
        job_queue=job_queue if job_queue is not None else FakeJobQueue(),
        bot=SimpleNamespace(
            edit_message_text=AsyncMock(),
            send_chat_action=AsyncMock(),
            send_message=AsyncMock(),
        ),
    )


def replies(update) -> list[str]:
    return [c.args[0] for c in update.message.reply_text.await_args_list]


def edited(update) -> str:
    return update.callback_query.edit_message_text.await_args.args[0]


def popup(update) -> str | None:
    call = update.callback_query.answer.await_args
    return call.args[0] if call.args and call.kwargs.get("show_alert") else None
