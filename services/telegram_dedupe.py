"""In-memory dedupe for Telegram webhook updates (retry protection)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

# Telegram may retry the same update while Gmail+LLM work is still running.
_DEDUPE_TTL_SECONDS = 600
_MAX_ENTRIES = 2000
_seen: OrderedDict[int, float] = OrderedDict()
_seen_str: OrderedDict[str, float] = OrderedDict()
_lock = asyncio.Lock()

# Outbound alert dedupe: suppress identical text to the same chat within TTL.
_CONTENT_TTL_SECONDS = 600
_CONTENT_MAX_ENTRIES = 2000
_content_seen: OrderedDict[tuple[str, str], float] = OrderedDict()
_content_lock = asyncio.Lock()


def _content_key(chat_id: str | int, text: str) -> tuple[str, str]:
    digest = hashlib.blake2b(str(text).encode("utf-8"), digest_size=16).hexdigest()
    return str(chat_id), digest


def _prune_content_seen(now: float) -> None:
    expired = [key for key, ts in _content_seen.items() if now - ts > _CONTENT_TTL_SECONDS]
    for key in expired:
        _content_seen.pop(key, None)
    while len(_content_seen) > _CONTENT_MAX_ENTRIES:
        _content_seen.popitem(last=False)


async def should_process_update(update_id: int | None) -> bool:
    """Return False if this update_id was already seen recently."""
    if update_id is None:
        return True

    now = time.monotonic()
    async with _lock:
        expired = [uid for uid, ts in _seen.items() if now - ts > _DEDUPE_TTL_SECONDS]
        for uid in expired:
            _seen.pop(uid, None)

        if update_id in _seen:
            return False

        _seen[update_id] = now
        while len(_seen) > _MAX_ENTRIES:
            _seen.popitem(last=False)
        return True


async def should_process_webhook_event(event_id: str | None) -> bool:
    """Dedupe non-Telegram webhooks (e.g. WhatsApp message.id).

    Memory covers this process. webhook_events covers a restart, when Meta
    retries the same message id and would otherwise send the menu twice.
    """
    if not event_id:
        return True
    key = str(event_id)
    now = time.monotonic()
    async with _lock:
        expired = [uid for uid, ts in _seen_str.items() if now - ts > _DEDUPE_TTL_SECONDS]
        for uid in expired:
            _seen_str.pop(uid, None)
        if key in _seen_str:
            return False

    try:
        from services.db import webhook_event_seen

        seen = await asyncio.to_thread(webhook_event_seen, key)
    except Exception:
        seen = None
    if seen is True:
        async with _lock:
            _seen_str[key] = now
        return False

    async with _lock:
        if key in _seen_str:
            return False
        _seen_str[key] = now
        while len(_seen_str) > _MAX_ENTRIES:
            _seen_str.popitem(last=False)
        return True


async def forget_webhook_event(event_id: str | None) -> None:
    """Allow a retry after handling failed. A stored success is not forgotten."""
    if not event_id:
        return
    async with _lock:
        _seen_str.pop(str(event_id), None)


async def remember_webhook_event(event_id: str | None) -> None:
    """Persist a successfully handled id so a restart does not answer it twice."""
    if not event_id:
        return
    try:
        from services.db import claim_webhook_event

        await asyncio.to_thread(claim_webhook_event, str(event_id))
    except Exception:
        logger.debug("webhook event persist failed", exc_info=True)


async def already_sent_content(chat_id: str | int, text: str) -> bool:
    """Return True when identical text was recently sent to this chat."""
    if not chat_id or not text or not str(text).strip():
        return False

    key = _content_key(chat_id, text)
    now = time.monotonic()
    async with _content_lock:
        _prune_content_seen(now)
        return key in _content_seen


async def record_content_sent(chat_id: str | int, text: str) -> None:
    """Record a successful outbound alert after delivery."""
    if not chat_id or not text or not str(text).strip():
        return

    key = _content_key(chat_id, text)
    now = time.monotonic()
    async with _content_lock:
        _prune_content_seen(now)
        _content_seen[key] = now
        while len(_content_seen) > _CONTENT_MAX_ENTRIES:
            _content_seen.popitem(last=False)
