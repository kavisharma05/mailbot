"""Broadcast real-time events to connected web clients (scoped per user when known)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_connections: dict[str, set[WebSocket]] = {}
_ws_user: dict[WebSocket, str] = {}
_lock = asyncio.Lock()


async def connect(ws: WebSocket, user_id: str | None = None) -> None:
    await ws.accept()
    uid = user_id or "_anonymous"
    async with _lock:
        _connections.setdefault(uid, set()).add(ws)
        _ws_user[ws] = uid
    await broadcast("sync_status", {"status": "connected"}, user_id=user_id)


async def disconnect(ws: WebSocket) -> None:
    async with _lock:
        uid = _ws_user.pop(ws, None)
        if uid and uid in _connections:
            _connections[uid].discard(ws)
            if not _connections[uid]:
                del _connections[uid]


async def broadcast(
    event: str, payload: dict[str, Any] | None = None, *, user_id: str | None = None
) -> None:
    message = json.dumps({"event": event, "data": payload or {}})
    async with _lock:
        if user_id:
            targets = list(_connections.get(user_id, []))
        else:
            targets = [ws for bucket in _connections.values() for ws in bucket]
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
                await disconnect(ws)


def broadcast_fire_and_forget(
    event: str, payload: dict[str, Any] | None = None, *, user_id: str | None = None
) -> None:
    """Schedule a broadcast from sync code (e.g. watcher thread)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broadcast(event, payload, user_id=user_id))
