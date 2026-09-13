"""Persist WhatsApp user_data in Supabase (same table as Telegram PTB persistence)."""

from __future__ import annotations

import asyncio
import copy
import time

from services.db import delete_user_data_state, load_user_data_state, save_user_data_state

_WA_PREFIX = "wa:"
_CACHE_TTL_SECONDS = 45.0
_cache: dict[str, tuple[float, dict]] = {}


def wa_user_id(phone: str) -> str:
    normalized = str(phone or "").lstrip("+").replace(" ", "").replace("-", "")
    return f"{_WA_PREFIX}{normalized}"


def load_user_data_sync(phone: str) -> dict:
    uid = wa_user_id(phone)
    now = time.monotonic()
    hit = _cache.get(uid)
    if hit and now - hit[0] < _CACHE_TTL_SECONDS:
        return copy.deepcopy(hit[1])
    data = load_user_data_state(uid)
    loaded = dict(data) if isinstance(data, dict) else {}
    _cache[uid] = (now, copy.deepcopy(loaded))
    return loaded


async def load_user_data(phone: str) -> dict:
    return await asyncio.to_thread(load_user_data_sync, phone)


async def save_user_data(phone: str, data: dict) -> None:
    uid = wa_user_id(phone)
    payload = dict(data) if isinstance(data, dict) else {}
    _cache[uid] = (time.monotonic(), copy.deepcopy(payload))
    await asyncio.to_thread(save_user_data_state, uid, payload)


async def drop_user_data(phone: str) -> None:
    uid = wa_user_id(phone)
    _cache.pop(uid, None)
    await asyncio.to_thread(delete_user_data_state, uid)
