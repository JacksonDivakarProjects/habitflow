"""HTTP calls to the LLM service. Any failure raises LLMUnavailable, so callers
have one thing to catch before falling back to their offline path."""

import httpx

from app.config import settings


class LLMUnavailable(Exception):
    pass


def post(path: str, payload: dict) -> dict:
    try:
        r = httpx.post(
            f"{settings.llm_base_url}{path}",
            json=payload,
            timeout=settings.llm_timeout_seconds,
        )
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LLMUnavailable(f"{path}: {e}") from e
