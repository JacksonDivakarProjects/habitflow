"""LLM service tests. The Groq client is replaced; no network calls are made."""

import json
import os
from types import SimpleNamespace

import pytest

# app.config reads these at import time.
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ.setdefault("APP_TIMEZONE", "Asia/Kolkata")


class FakeCompletions:
    def __init__(self):
        self.replies: list = []
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


@pytest.fixture
def groq(monkeypatch):
    from app import client

    fake = FakeCompletions()
    monkeypatch.setattr(client.client.chat, "completions", fake)
    return fake


HABITS = [
    {
        "name": "running",
        "display_name": "Running",
        "default_metric": "miles",
        "recent_metric": "km",
    },
    {
        "name": "reading",
        "display_name": "Reading",
        "default_metric": None,
        "recent_metric": None,
    },
]
