"""Multi-provider LLM client with cooldowns and failover."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx
from groq import APIStatusError, Groq

try:
    from curl_cffi import requests as _curl_requests
except ImportError:  # pragma: no cover - requirements include it, fallback is for safety
    _curl_requests = None

from config import get_settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 2
_RETRY_STATUS = {429, 500, 502, 503}

_CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_GITHUB_MODELS_URL = "https://models.github.ai/inference/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_CLAUDE_BASE_URL = "https://claude.ai"
_CLAUDE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

# Shared keep-alive client: a fresh httpx.post() does a TLS handshake per call
# (~150-400ms). Fail over faster than 60s so a hung provider doesn't stall the user.
_LLM_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_shared_http: httpx.Client | None = None
_groq_clients: dict[str, Groq] = {}


def _http() -> httpx.Client:
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.Client(
            timeout=_LLM_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120),
        )
    return _shared_http


def _groq_client(api_key: str) -> Groq:
    client = _groq_clients.get(api_key)
    if client is None:
        try:
            client = Groq(api_key=api_key, timeout=12.0)
        except TypeError:
            client = Groq(api_key=api_key)
        _groq_clients[api_key] = client
    return client

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
    extra_payload: dict[str, Any] | None = None,
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
    if extra_payload:
        payload.update(extra_payload)

    for attempt in range(1, 3):
        try:
            resp = _http().post(url, headers=headers, json=payload)
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
                elif resp.status_code == 402:
                    # Payment/quota exhausted — back off for an hour, don't hammer.
                    _cooldown(provider, seconds=3600)
                elif resp.status_code == 404:
                    # Model retired/renamed — back off for hours, not seconds.
                    _cooldown(provider, seconds=6 * 3600)
                return None

            data = resp.json()
            message = (data.get("choices") or [{}])[0].get("message", {}) or {}
            raw_content = message.get("content")
            if isinstance(raw_content, list):
                # Some Gemini OpenAI-compat shapes return content parts
                raw_content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in raw_content
                )
            text = (raw_content or "").strip()
            if text:
                logger.info("LLM answered via %s model=%s", provider, model)
                _mark_ok(provider)
                return text
            # Never serve hidden chain-of-thought (reasoning field) as the answer:
            # reasoning models can exhaust max_tokens before producing content.
            _set_last_error("empty content (reasoning-only response)", resp.status_code, provider)
            logger.warning(
                "%s returned empty content (reasoning-only?) model=%s", provider, model
            )
            return None
        except Exception as exc:
            if attempt < 2:
                time.sleep(1)
                continue
            _set_last_error(str(exc), None, provider)
            logger.error("%s exception: %s", provider, exc)
            # Repeated connection/SSL failures shouldn't be retried every call.
            _cooldown(provider, seconds=300)
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

    client = _groq_client(api_key)
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
            if "gpt-oss" in model:
                # gpt-oss is a reasoning model — keep reasoning lean so hidden
                # chain-of-thought can't starve the answer budget (or quota).
                kwargs["reasoning_effort"] = "low"

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
            if exc.status_code == 402:
                _cooldown(provider, seconds=3600)
                logger.error("%s payment/quota exhausted: %s", provider, exc)
                return None
            if exc.status_code == 404:
                # Model retired/renamed — back off for hours, not seconds.
                _cooldown(provider, seconds=6 * 3600)
                logger.error("%s model not found (retired?): %s", provider, exc)
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
            _cooldown(provider, seconds=300)
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
        extra_payload={"reasoning": {"exclude": True}},
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
    # Reasoning models burn a large share of max_tokens on hidden chain-of-thought
    # before emitting the answer — give them headroom so content isn't truncated.
    safe_max_tokens = max(max_tokens * 3, 1500)
    return _openai_compatible_post(
        url=_OPENROUTER_URL,
        api_key=settings.nemotron_api_key,
        model=model,
        system=system,
        user=user,
        json_mode=json_mode,
        max_tokens=safe_max_tokens,
        temperature=temperature,
        provider="nemotron",
        extra_headers={
            "HTTP-Referer": settings.render_url or "https://mailbot.local",
            "X-Title": "Mailbot",
        },
        extra_payload={"reasoning": {"exclude": True}},
    )


def _claude_session_configured(settings: Any) -> bool:
    return bool(
        settings.claude_session_enabled
        and settings.claude_org_uuid
        and (settings.claude_session_key or settings.claude_cookie_header)
    )


def _claude_cookie_header(settings: Any) -> str:
    """Build a complete browser Cookie header without logging any secret values."""
    parts: list[str] = []
    seen: set[str] = set()
    raw = (settings.claude_cookie_header or "").strip()
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name = item.split("=", 1)[0].strip()
        if not name:
            continue
        seen.add(name)
        parts.append(item)
    if settings.claude_session_key and "sessionKey" not in seen:
        parts.append(f"sessionKey={settings.claude_session_key.strip()}")
        seen.add("sessionKey")
    if settings.claude_org_uuid and "lastActiveOrg" not in seen:
        parts.append(f"lastActiveOrg={settings.claude_org_uuid.strip()}")
    return "; ".join(parts)


def _claude_headers(settings: Any, *, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Content-Type": "application/json",
        "Cookie": _claude_cookie_header(settings),
        "Origin": _CLAUDE_BASE_URL,
        "Referer": f"{_CLAUDE_BASE_URL}/",
        "User-Agent": _CLAUDE_USER_AGENT,
    }


def _claude_request(method: str, url: str, **kwargs: Any):
    """HTTP request with Chrome TLS impersonation when curl_cffi is available.

    Claude.ai sits behind Cloudflare; plain httpx often receives a 403 challenge
    even with a valid sessionKey. curl_cffi's Chrome fingerprint matches the
    browser that produced the cookie. httpx remains a safe fallback.
    """
    if _curl_requests is not None:
        kwargs.setdefault("impersonate", "chrome")
        return _curl_requests.request(method, url, **kwargs)
    return _http().request(method, url, **kwargs)


def _claude_error(resp: Any, *, provider: str) -> None:
    body = (resp.text or "")[:500]
    _set_last_error(body, resp.status_code, provider)
    logger.error("claude_session failed (%s): %s", resp.status_code, body[:300])
    if resp.status_code == 429 or _is_daily_limit_text(body):
        _cooldown(provider, daily=_is_daily_limit_text(body), seconds=180)
    elif resp.status_code in (401, 403):
        _cooldown(provider, seconds=90)
    elif resp.status_code == 402:
        _cooldown(provider, seconds=3600)


def _extract_claude_stream_text(body: str) -> str:
    """Parse Claude web SSE (and occasional JSON/non-SSE fallback) into plain text."""
    chunks: list[str] = []
    saw_event = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        saw_event = True
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            text = delta.get("text") or delta.get("partial_json") or ""
            if text:
                chunks.append(str(text))
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("text"):
                chunks.append(str(block["text"]))
        elif isinstance(event.get("completion"), str):
            chunks.append(event["completion"])
        elif event_type == "message_delta":
            delta = event.get("delta") or {}
            if isinstance(delta.get("content"), str):
                chunks.append(delta["content"])
        elif event_type == "error":
            message = (event.get("error") or {}).get("message") or str(event)
            raise ValueError(message)

    if chunks:
        return "".join(chunks).strip()
    if saw_event:
        return ""

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()
    if isinstance(data, dict):
        if isinstance(data.get("completion"), str):
            return data["completion"].strip()
        if isinstance(data.get("text"), str):
            return data["text"].strip()
        content = data.get("content")
        if isinstance(content, list):
            texts = [
                str(part.get("text"))
                for part in content
                if isinstance(part, dict) and part.get("text")
            ]
            return "".join(texts).strip()
    return ""


def _delete_claude_conversation(settings: Any, conversation_id: str) -> None:
    """Best-effort cleanup so one-shot bot calls do not clutter Claude history."""
    org = quote(settings.claude_org_uuid, safe="")
    conv = quote(conversation_id, safe="")
    url = f"{_CLAUDE_BASE_URL}/api/organizations/{org}/chat_conversations/{conv}"
    try:
        _claude_request(
            "DELETE",
            url,
            headers=_claude_headers(settings, accept="application/json"),
            timeout=10,
        )
    except Exception as exc:
        logger.debug("claude_session cleanup failed: %s", exc)


def _call_claude_session(
    system: str,
    user: str,
    *,
    json_mode: bool,
    max_tokens: int,
    temperature: float,
) -> str | None:
    """Use the single configured Claude.ai browser session as an additive provider.

    This intentionally lives behind CLAUDE_SESSION_ENABLED and uses the same
    cooldown/failover conventions as the free providers. The web endpoint does
    not expose max_tokens/temperature, so prompt instructions carry JSON mode.
    """
    del max_tokens, temperature
    settings = get_settings()
    provider = "claude_session"
    if not _claude_session_configured(settings) or _is_cooled(provider):
        return None

    org = quote(settings.claude_org_uuid.strip(), safe="")
    base = f"{_CLAUDE_BASE_URL}/api/organizations/{org}/chat_conversations"
    conversation_id = ""
    try:
        created = _claude_request(
            "POST",
            base,
            headers=_claude_headers(settings, accept="application/json"),
            json={"uuid": None, "name": "", "model": settings.claude_model},
            timeout=12,
        )
        if created.status_code >= 400:
            _claude_error(created, provider=provider)
            return None
        data = created.json()
        conversation_id = str(data.get("uuid") or data.get("conversation_uuid") or "")
        if not conversation_id:
            _set_last_error("missing Claude conversation uuid", None, provider)
            return None

        prompt = f"{system.strip()}\n\n{user.strip()}"
        if json_mode:
            prompt += "\n\nRespond ONLY with valid strict JSON. No markdown fences or prose."
        completion = _claude_request(
            "POST",
            f"{base}/{quote(conversation_id, safe='')}/completion",
            headers=_claude_headers(settings, accept="text/event-stream"),
            json={
                "prompt": prompt,
                "model": settings.claude_model,
                "timezone": "Asia/Kolkata",
                "attachments": [],
                "files": [],
            },
            timeout=45,
        )
        if completion.status_code >= 400:
            _claude_error(completion, provider=provider)
            return None
        text = _extract_claude_stream_text(completion.text)
        if text:
            logger.info("LLM answered via claude_session model=%s", settings.claude_model)
            _mark_ok(provider)
            return text
        _set_last_error("empty Claude session response", completion.status_code, provider)
        return None
    except Exception as exc:
        _set_last_error(str(exc), None, provider)
        logger.error("claude_session exception: %s", exc)
        _cooldown(provider, seconds=300)
        return None
    finally:
        if conversation_id:
            _delete_claude_conversation(settings, conversation_id)


ProviderFn = Callable[..., str | None]


def _provider_chain(json_mode: bool = False) -> list[tuple[str, ProviderFn]]:
    """Groq first for speed; Claude session next so it is actually tried
    when the free APIs fail instead of sitting unused at the end.
    """
    settings = get_settings()
    chain: list[tuple[str, ProviderFn]] = []

    def add_claude_session() -> None:
        if _claude_session_configured(settings):
            chain.append(("claude_session", _call_claude_session))

    def add_nemotron() -> None:
        if settings.nemotron_enabled and settings.nemotron_api_key:
            chain.append(("nemotron", _call_nemotron))

    def add_groq() -> None:
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

    def add_gemini() -> None:
        if settings.gemini_api_key:
            chain.append(("gemini", _call_gemini))

    def add_cerebras() -> None:
        if settings.cerebras_enabled and settings.cerebras_api_key:
            chain.append(("cerebras", _call_cerebras))

    def add_openrouter() -> None:
        if settings.openrouter_api_key:
            chain.append(("openrouter", _call_openrouter))

    def add_github_models() -> None:
        if settings.github_models_enabled and settings.github_models_token:
            chain.append(("github_models", _call_github_models))

    add_groq()
    add_gemini()
    add_openrouter()
    add_claude_session()
    add_cerebras()
    add_nemotron()
    add_github_models()
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
    validator: Callable[[str], bool] | None = None,
) -> str | None:
    """Try configured providers in order; skip cooled-down ones.

    When ``validator`` is given, a provider whose response fails validation is
    skipped (brief cooldown) and the next provider is tried — a model that
    returns unusable output (e.g. non-JSON for a JSON call) no longer poisons
    the whole request.
    """
    _set_last_error(None, None, None)
    # `model` arg kept for API compat; per-provider env models are used.
    _ = model

    attempted = False
    for name, fn in _provider_chain(json_mode=json_mode):
        if _is_cooled(name):
            logger.info("Skipping %s (cooldown)", name)
            continue
        attempted = True
        logger.info("Trying LLM provider %s", name)
        text = fn(
            system,
            user,
            json_mode=json_mode,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not text:
            continue
        if validator is not None and not validator(text):
            logger.warning(
                "Provider %s returned unusable payload — trying next provider", name
            )
            _set_last_error("unusable payload", None, name)
            _cooldown(name, seconds=45)
            continue
        return text
    if not attempted:
        # Every provider was skipped due to cooldown — keep this visible in
        # /healthz instead of silently returning None with a clean status.
        _set_last_error("all providers in cooldown", None, None)
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


def call_json(
    system: str,
    user: str,
    model: str | None = None,
    *,
    max_tokens: int = 600,
) -> dict[str, Any] | None:
    """Call LLM and parse strict JSON from the response.

    Providers that return unparseable output are skipped in favour of the next
    provider in the chain (see _complete's validator).
    """
    text = _complete(
        system,
        user,
        json_mode=True,
        max_tokens=max_tokens,
        model=model,
        temperature=0.2,
        validator=lambda t: _parse_json_text(t) is not None,
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
    max_tokens: int = 600,
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
