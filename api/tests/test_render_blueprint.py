"""render.yaml is committed to a public repo: secrets must never be written in it."""

import re
from pathlib import Path

import yaml

BLUEPRINT = Path(__file__).resolve().parents[2] / "render.yaml"
SECRETS = {"DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID", "GROQ_API_KEY"}


def _env_vars():
    spec = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    return [v for s in spec["services"] for v in s.get("envVars", [])]


def test_secrets_are_prompted_not_written():
    by_key = {v["key"]: v for v in _env_vars()}
    for key in SECRETS:
        assert by_key[key].get("sync") is False, f"{key} must be `sync: false`"
        assert "value" not in by_key[key], f"{key} has a value written in render.yaml"


def test_no_credentials_anywhere_in_the_file():
    text = BLUEPRINT.read_text(encoding="utf-8")
    # a connection string with a real password (placeholders like <password> are fine)
    assert not re.search(r"postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:(?!<)[^@\s]+@", text)
    assert not re.search(r"\bnpg_[A-Za-z0-9]{6,}", text)          # Neon password
    assert not re.search(r"\bgsk_[A-Za-z0-9]{10,}", text)         # Groq key
    assert not re.search(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b", text)  # Telegram token


def test_worker_region_is_explicit():
    spec = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    assert spec["services"][0]["region"] in {"oregon", "ohio", "virginia", "frankfurt", "singapore"}
