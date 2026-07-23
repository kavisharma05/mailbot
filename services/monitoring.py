"""External heartbeat pings and retry helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    label: str = "operation",
) -> T:
    """Run an async callable with exponential backoff on failure."""
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning("%s attempt %s/%s failed: %s", label, attempt, max_retries, exc)
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def ping_heartbeat(*, ok: bool = True) -> bool:
    """Ping healthchecks.io (or compatible) on successful runs. No-op when unset."""
    settings = get_settings()
    url = (settings.heartbeat_url or "").strip()
    if not url:
        return False

    if not ok:
        url = url.rstrip("/") + "/fail"

    async def _do_ping() -> bool:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return True

    try:
        await retry_with_backoff(_do_ping, label="heartbeat")
        logger.info("Heartbeat ping ok")
        return True
    except Exception as exc:
        logger.warning("Heartbeat ping failed: %s", exc)
        return False
