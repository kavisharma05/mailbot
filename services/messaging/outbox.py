"""Track outbound WhatsApp message IDs so async status callbacks stay actionable.

Meta sometimes accepts a free-form message (HTTP 200) and then fails delivery
asynchronously with a ``failed`` status webhook (e.g. error 131047 — outside
the 24h customer-service window). Remembering ``wamid -> text`` lets the
webhook handler stash the content and nudge the owner with an approved
template instead of silently dropping the alert.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.messaging.state import load_user_data, save_user_data

logger = logging.getLogger(__name__)

OUTBOX_KEY = "wa_outbox"
_MAX_ENTRIES = 25
_TTL_SECONDS = 48 * 3600


def _parse_ts(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except ValueError:
        return None


def _prune(entries: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).timestamp()
    fresh: dict[str, dict[str, Any]] = {}
    for wamid, item in entries.items():
        if not isinstance(item, dict) or not item.get("text"):
            continue
        ts = _parse_ts(item.get("ts"))
        if ts is None or now - ts > _TTL_SECONDS:
            continue
        fresh[str(wamid)] = item
    if len(fresh) > _MAX_ENTRIES:
        ordered = sorted(fresh.items(), key=lambda kv: str(kv[1].get("ts") or ""))
        fresh = dict(ordered[-_MAX_ENTRIES:])
    return fresh


async def register_outbound(
    phone: str,
    wamids: list[str],
    text: str,
    *,
    kind: str = "alert",
) -> None:
    """Remember which text each accepted message id carries."""
    ids = [str(w) for w in wamids if w]
    if not ids or not (text or "").strip():
        return
    try:
        data = await load_user_data(phone)
        entries = _prune(dict(data.get(OUTBOX_KEY) or {}))
        stamp = datetime.now(timezone.utc).isoformat()
        for wamid in ids:
            entries[wamid] = {"text": text, "kind": kind, "ts": stamp}
        data[OUTBOX_KEY] = entries
        await save_user_data(phone, data)
    except Exception as exc:  # never break sending on bookkeeping
        logger.debug("WhatsApp outbox register failed for %s: %s", phone, exc)


async def take_outbound(phone: str, wamid: str) -> dict[str, Any] | None:
    """Pop the tracked payload for a message id (one-shot)."""
    if not wamid:
        return None
    try:
        data = await load_user_data(phone)
        entries = _prune(dict(data.get(OUTBOX_KEY) or {}))
        item = entries.pop(str(wamid), None)
        data[OUTBOX_KEY] = entries
        await save_user_data(phone, data)
    except Exception as exc:
        logger.debug("WhatsApp outbox take failed for %s: %s", phone, exc)
        return None
    return item if isinstance(item, dict) and item.get("text") else None
