"""The committed .env examples must document every setting and never hold secrets."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = [ROOT / ".env.example", ROOT / ".env.single.example"]
SECRET_PATTERNS = [
    r"npg_[A-Za-z0-9]{8,}",                               # Neon password
    r"gsk_[A-Za-z0-9]{10,}",                              # Groq key
    r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b",                     # Telegram bot token
    r"postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:(?!<)[^@\s]+@",  # URL with a real password
]


def _assignments(path: Path) -> dict[str, str]:
    """Active KEY=value lines (comments ignored)."""
    pairs = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    return pairs


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_holds_no_secrets(path):
    text = path.read_text(encoding="utf-8")
    for pattern in SECRET_PATTERNS:
        assert not re.search(pattern, text), f"{path.name} contains a secret ({pattern})"


def test_single_example_lists_the_required_settings_empty():
    values = _assignments(ROOT / ".env.single.example")
    for key in ("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_ID", "GROQ_API_KEY"):
        assert key in values, f"{key} missing from .env.single.example"
        assert values[key] == "", f"{key} must be empty in the example"


def test_single_example_documents_every_supervisor_setting():
    """Each env var run_all.py reads (except platform/internal ones) is explained."""
    code = (ROOT / "allinone" / "run_all.py").read_text(encoding="utf-8")
    read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', code))
    internal = {"PORT", "RENDER", "RENDER_EXTERNAL_URL", "API_HOST", "DATA_DIR", "PG_BIN",
                "STARTUP_HEALTH_TIMEOUT"}
    doc = (ROOT / ".env.single.example").read_text(encoding="utf-8")
    missing = sorted(k for k in read - internal if k not in doc)
    assert not missing, f"undocumented in .env.single.example: {missing}"


@pytest.mark.parametrize("name, ignored", [
    (".env", True), (".env.single", True), (".env.local", True),
    (".env.example", False), (".env.single.example", False),
])
def test_filled_in_env_files_are_gitignored(name, ignored):
    r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT)
    assert (r.returncode == 0) is ignored
