"""Free multi-provider LLM client with cooldowns and failover."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from groq import APIStatusError, Groq

from config import get_settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_STATUS = {429, 500, 502, 503}

_CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_last_error: str | None = None
_last_status: int | None = None
_last_provider: str | None = None
_last_ok_provider: str | None = None

# provider_name -> unix timestamp when cooldown ends
_cooldowns: dict[str, float] = {}


def get_last_groq_error() -> tuple[str | None, int | None]:
    """Backward-compatible alias for get_last_llm_error (status only)."""
    return _last_error, _last_status


def get_last_llm_error() -> tuple[str | None, int | None, str | None]:
    return _last_error, _last_status, _last_provider


def get_llm_status() -> dict[str, Any]:
    """Safe status for /healthz (no secrets)."""
    now = time.time()
    cooled = {
        name: int(max(0, until - now))
        for name, until in _cooldowns.items()
        if until > now
    }
    settings = get_settings()
    nemotron: dict[str, str] | None = None
    if settings.nemotron_api_key:
        nemotron = {
            "primary_model": settings.nemotron_primary_model,
            "brain_model": settings.nemotron_brain_model,
        }
    return {
        "last_ok_provider": _last_ok_provider,
        "last_error_provider": _last_provider if _last_error else None,
        "last_error_status": _last_status if _last_error else None,
        "cooldowns_seconds": cooled,
        "providers_configured": _configured_provider_names(),
        "nemotron": nemotron,
    }


def _set_last_error(
    message: str | None,
    status: int | None = None,
    provider: str | None = None,
) -> None:
    global _last_error, _last_status, _last_provider
    _last_error = message
    _last_status = status
    _last_provider = provider


def _mark_ok(provider: str) -> None:
    global _last_ok_provider
    _last_ok_provider = provider
    _set_last_error(None, None, provider)
    _cooldowns.pop(provider, None)


def _seconds_until_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0)).timestamp() + 86400
    return max(60.0, tomorrow - now.timestamp())


def _cooldown(provider: str, seconds: float | None = None, *, daily: bool = False) -> None:
    if daily:
        seconds = _seconds_until_utc_midnight()
    else:
        seconds = float(seconds or 120)
    until = time.time() + seconds
    prev = _cooldowns.get(provider, 0)
    _cooldowns[provider] = max(prev, until)
    logger.warning("Provider %s cooling down for %.0fs", provider, seconds)


def _is_cooled(provider: str) -> bool:
    until = _cooldowns.get(provider, 0)
    if until <= time.time():
        _cooldowns.pop(provider, None)
        return False
    return True


def _is_daily_limit_text(text: str) -> bool:
    t = text.lower()
    return (
        "tokens per day" in t
        or "tpd" in t
        or "quota exceeded" in t
        or "exceeded your current quota" in t
        or ("rate_limit" in t and "day" in t)
        or "daily" in t and ("limit" in t or "quota" in t)
    )


def _openai_compatible_post(
    *,
    url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
    provider: str,
    extra_headers: dict[str, str] | None = None,
) -> str | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        # Some providers ignore this; prompts still request JSON.
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(1, 3):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=60)
            if resp.status_code in _RETRY_STATUS and attempt < 2:
                delay = min(2 ** (attempt - 1), 2)
                logger.warning(
                    "%s transient %s (attempt %d/2), retry in %ds",
                    provider,
                    resp.status_code,
                    attempt,
                    delay,
                )
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                body = resp.text[:400]
                _set_last_error(body, resp.status_code, provider)
                logger.error("%s failed (%s): %s", provider, resp.status_code, body[:300])
                if resp.status_code == 429 or _is_daily_limit_text(body):
                    _cooldown(provider, daily=_is_daily_limit_text(body), seconds=180)
                elif resp.status_code in (401, 403):
                    _cooldown(provider, seconds=3600)
                return None

            data = resp.json()
            message = (data.get("choices") or [{}])[0].get("message", {}) or {}
            text = (message.get("content") or message.get("reasoning") or "").strip()
            if isinstance(text, list):
                # Some Gemini OpenAI-compat shapes return content parts
                text = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in text
                ).strip()
            if text:
                logger.info("LLM answered via %s model=%s", provider, model)
                _mark_ok(provider)
                return text
            _set_last_error("empty response", resp.status_code, provider)
            return None
        except Exception as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            _set_last_error(str(exc), None, provider)
            logger.error("%s exception: %s", provider, exc)
            return None
    return None


def _call_groq(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
    api_key: str,
    model: str,
    provider: str,
) -> str | None:
    if not api_key:
        return None
    if _is_cooled(provider):
        return None

    client = Groq(api_key=api_key)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}

            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            if text:
                _mark_ok(provider)
            return text or None
        except APIStatusError as exc:
            text = str(exc)
            _set_last_error(text, exc.status_code, provider)
            if exc.status_code == 429 or _is_daily_limit_text(text):
                _cooldown(provider, daily=_is_daily_limit_text(text), seconds=180)
                logger.error("%s rate/daily limit: %s", provider, exc)
                return None
            if exc.status_code in (401, 403):
                _cooldown(provider, seconds=3600)
                logger.error("%s auth failed (%s): %s", provider, exc.status_code, exc)
                return None
            if exc.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                delay = min(2 ** (attempt - 1), 2)
                logger.warning(
                    "%s transient %s (attempt %d/%d), retry in %ds",
                    provider,
                    exc.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.error("%s failed: %s", provider, exc)
            return None
        except Exception as exc:
            _set_last_error(str(exc), None, provider)
            logger.error("%s exception: %s", provider, exc)
            return None
    return None


def _call_cerebras(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    settings = get_settings()
    if not settings.cerebras_api_key or _is_cooled("cerebras"):
        return None
    return _openai_compatible_post(
        url=_CEREBRAS_URL,
        api_key=settings.cerebras_api_key,
        model=settings.cerebras_model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
        provider="cerebras",
    )


def _call_gemini(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    settings = get_settings()
    if not settings.gemini_api_key or _is_cooled("gemini"):
        return None
    return _openai_compatible_post(
        url=_GEMINI_URL,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
        provider="gemini",
    )


def _call_github_models(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    settings = get_settings()
    token = settings.github_models_token
    if not token or _is_cooled("github_models"):
        return None
    return _openai_compatible_post(
        url=_GITHUB_MODELS_URL,
        api_key=token,
        model=settings.github_models_model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
        provider="github_models",
    )


def _call_openrouter(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    settings = get_settings()
    if not settings.openrouter_api_key or _is_cooled("openrouter"):
        return None
    return _openai_compatible_post(
        url=_OPENROUTER_URL,
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
        provider="openrouter",
        extra_headers={
            "HTTP-Referer": settings.render_url or "https://mailbot.local",
            "X-Title": "Mailbot",
        },
    )


def _call_nemotron(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    settings = get_settings()
    if not settings.nemotron_api_key or _is_cooled("nemotron"):
        return None
    model = (
        settings.nemotron_primary_model
        if json_mode
        else settings.nemotron_brain_model
    )
    return _openai_compatible_post(
        url=_OPENROUTER_URL,
        api_key=settings.nemotron_api_key,
        model=model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=max_tokens,
        temperature=temperature,
        provider="nemotron",
        extra_headers={
            "HTTP-Referer": settings.render_url or "https://mailbot.local",
            "X-Title": "Mailbot",
        },
    )


ProviderFn = Callable[..., str | None]


def _provider_chain() -> list[tuple[str, ProviderFn]]:
    settings = get_settings()
    chain: list[tuple[str, ProviderFn]] = []

    if settings.nemotron_api_key:
        chain.append(("nemotron", _call_nemotron))
    if settings.groq_api_key:
        chain.append(
            (
                "groq",
                lambda system, user, json_mode, max_tokens, temperature: _call_groq(
                    system,
                    user,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    api_key=settings.groq_api_key,
                    model=settings.groq_model,
                    provider="groq",
                ),
            )
        )
    if settings.groq_api_key_2:
        chain.append(
            (
                "groq_2",
                lambda system, user, json_mode, max_tokens, temperature: _call_groq(
                    system,
                    user,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    api_key=settings.groq_api_key_2,
                    model=settings.groq_model,
                    provider="groq_2",
                ),
            )
        )
    if settings.cerebras_api_key:
        chain.append(("cerebras", _call_cerebras))
    if settings.gemini_api_key:
        chain.append(("gemini", _call_gemini))
    if settings.github_models_token:
        chain.append(("github_models", _call_github_models))
    if settings.openrouter_api_key:
        chain.append(("openrouter", _call_openrouter))
    return chain


def _configured_provider_names() -> list[str]:
    return [name for name, _ in _provider_chain()]


def _complete(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    model: str | None = None,
    temperature: float = 0.3,
) -> str | None:
    """Try free providers in order; skip cooled-down ones."""
    _set_last_error(None, None, None)
    # `model` arg kept for API compat; per-provider env models are used.
    _ = model

    for name, fn in _provider_chain():
        if _is_cooled(name):
            logger.info("Skipping %s (cooldown)", name)
            continue
        logger.info("Trying LLM provider %s", name)
        text = fn(
            system,
            user,
            json_mode=json_mode,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if text:
            return text
    logger.error("All LLM providers failed or cooled down")
    return None


def _parse_json_text(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def call_json(system: str, user: str, model: str | None = None) -> dict[str, Any] | None:
    """Call LLM and parse strict JSON from the response."""
    text = _complete(
        system, user, json_mode=True, max_tokens=900, model=model, temperature=0.2
    )
    if not text:
        return None

    parsed = _parse_json_text(text)
    if parsed is None:
        logger.warning("Failed to parse JSON from LLM response: %s", text[:200])
    return parsed


def call_text(
    system: str,
    user: str,
    model: str | None = None,
    *,
    temperature: float = 0.55,
    max_tokens: int = 1000,
) -> str | None:
    """Call LLM and return plain text."""
    return _complete(
        system,
        user,
        json_mode=False,
        max_tokens=max_tokens,
        model=model,
        temperature=temperature,
    )
