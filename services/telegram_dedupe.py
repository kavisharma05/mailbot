"""In-memory dedupe for Telegram webhook updates (retry protection)."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

# Telegram may retry the same update while Gmail+LLM work is still running.
_DEDUPE_TTL_SECONDS = 600
_MAX_ENTRIES = 2000
_seen: OrderedDict[int, float] = OrderedDict()
_lock = asyncio.Lock()


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
