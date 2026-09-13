"""Persist WhatsApp user_data in Supabase (same table as Telegram PTB persistence)."""

from __future__ import annotations

import asyncio

from services.db import delete_user_data_state, save_user_data_state

_WA_PREFIX = "wa:"


def wa_user_id(phone: str) -> str:
    normalized = str(phone or "").lstrip("+").replace(" ", "").replace("-", "")
    return f"{_WA_PREFIX}{normalized}"


def load_user_data_sync(phone: str) -> dict:
    from services.db import load_all_user_data_states

    uid = wa_user_id(phone)
    all_states = load_all_user_data_states()
    data = all_states.get(uid)
    return dict(data) if isinstance(data, dict) else {}


async def load_user_data(phone: str) -> dict:
    return await asyncio.to_thread(load_user_data_sync, phone)


async def save_user_data(phone: str, data: dict) -> None:
    uid = wa_user_id(phone)
    await asyncio.to_thread(save_user_data_state, uid, data)


async def drop_user_data(phone: str) -> None:
    uid = wa_user_id(phone)
    await asyncio.to_thread(delete_user_data_state, uid)
