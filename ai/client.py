import json
import logging
import time
from typing import Any

import httpx
from groq import APIStatusError, Groq

from config import get_settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_RETRY_STATUS = {429, 500, 502, 503}
_CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

_last_error: str | None = None
_last_status: int | None = None
_last_provider: str | None = None


def get_last_groq_error() -> tuple[str | None, int | None]:
    """Backward-compatible alias for get_last_llm_error (status only)."""
    return _last_error, _last_status


def get_last_llm_error() -> tuple[str | None, int | None, str | None]:
    return _last_error, _last_status, _last_provider


def _set_last_error(
    message: str | None,
    status: int | None = None,
    provider: str | None = None,
) -> None:
    global _last_error, _last_status, _last_provider
    _last_error = message
    _last_status = status
    _last_provider = provider


def _is_daily_token_limit(exc: APIStatusError) -> bool:
    text = str(exc).lower()
    return "tokens per day" in text or "tpd" in text or "rate_limit_exceeded" in text


def _call_cerebras(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    model: str | None = None,
) -> str | None:
    """OpenAI-compatible Cerebras chat completion. Returns message content or None."""
    settings = get_settings()
    if not settings.cerebras_api_key:
        return None

    resolved_model = model or settings.cerebras_model
    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = httpx.post(
            _CEREBRAS_URL,
            headers={
                "Authorization": f"Bearer {settings.cerebras_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 400:
            _set_last_error(resp.text[:400], resp.status_code, "cerebras")
            logger.error("Cerebras call failed (%s): %s", resp.status_code, resp.text[:300])
            return None

        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {}) or {}
        # Some Cerebras models (e.g. gpt-oss) put text in reasoning with empty content
        text = (message.get("content") or message.get("reasoning") or "").strip()
        if text:
            logger.info("LLM answered via Cerebras model=%s", resolved_model)
            _set_last_error(None, None, "cerebras")
        return text or None
    except Exception as exc:
        _set_last_error(str(exc), None, "cerebras")
        logger.error("Cerebras call exception: %s", exc)
        return None


def _call_groq(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    model: str | None = None,
) -> str | None:
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)
    resolved_model = model or settings.groq_model

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": resolved_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if text:
                _set_last_error(None, None, "groq")
            return text or None
        except APIStatusError as exc:
            if exc.status_code == 429 and _is_daily_token_limit(exc):
                _set_last_error(str(exc), 429, "groq")
                logger.error("Groq daily token limit hit: %s", exc)
                return None
            if exc.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                delay = 2 ** (attempt - 1)
                logger.warning(
                    "Groq transient error %s (attempt %d/%d), retrying in %ds",
                    exc.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                continue
            _set_last_error(str(exc), exc.status_code, "groq")
            logger.error("Groq call failed: %s", exc)
            return None
    return None


def _complete(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    model: str | None = None,
) -> str | None:
    """Try Groq first; on failure (esp. rate limit), fall back to Cerebras."""
    _set_last_error(None, None, None)

    text = _call_groq(
        system,
        user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        model=model,
    )
    if text:
        return text

    logger.warning("Groq unavailable; trying Cerebras fallback")
    return _call_cerebras(
        system,
        user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        model=None,  # always use cerebras_model for fallback
    )


def call_json(system: str, user: str, model: str | None = None) -> dict[str, Any] | None:
    """Call LLM and parse strict JSON from the response (Groq → Cerebras)."""
    text = _complete(system, user, json_mode=True, max_tokens=512, model=model)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap JSON in fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
        logger.warning("Failed to parse JSON from LLM response: %s", text[:200])
        return None


def call_text(system: str, user: str, model: str | None = None) -> str | None:
    """Call LLM and return plain text (Groq → Cerebras)."""
    return _complete(system, user, json_mode=False, max_tokens=700, model=model)
