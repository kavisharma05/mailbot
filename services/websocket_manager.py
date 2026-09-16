"""Broadcast real-time events to connected web clients."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_connections: set[WebSocket] = set()
_lock = asyncio.Lock()


async def connect(ws: WebSocket) -> None:
    await ws.accept()
    async with _lock:
        _connections.add(ws)
    await broadcast("sync_status", {"status": "connected"})


async def disconnect(ws: WebSocket) -> None:
    async with _lock:
        _connections.discard(ws)


async def broadcast(event: str, payload: dict[str, Any] | None = None) -> None:
    message = json.dumps({"event": event, "data": payload or {}})
    async with _lock:
        targets = list(_connections)
    if not targets:
        return
    dead: list[WebSocket] = []
    for ws in targets:
        try:
            await ws.send_text(message)
        except Exception as exc:
            logger.debug("WebSocket send failed: %s", exc)
            dead.append(ws)
    if dead:
        async with _lock:
            for ws in dead:
                _connections.discard(ws)


def broadcast_fire_and_forget(event: str, payload: dict[str, Any] | None = None) -> None:
    """Schedule a broadcast from sync code (e.g. watcher thread)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broadcast(event, payload))
