"""Supabase-backed Telegram user_data persistence."""

from __future__ import annotations

import asyncio
import json
import logging

from telegram.ext import DictPersistence, PersistenceInput

from services.db import delete_user_data_state, load_all_user_data_states, save_user_data_state

logger = logging.getLogger(__name__)

DEFAULT_UPDATE_INTERVAL = 30.0


class SupabasePersistence(DictPersistence):
    """Persist conversation memory and compose drafts in Supabase."""

    def __init__(self, *, update_interval: float = DEFAULT_UPDATE_INTERVAL) -> None:
        store = PersistenceInput(
            user_data=True,
            chat_data=False,
            bot_data=False,
            callback_data=False,
        )
        all_states = load_all_user_data_states()
        user_data_json = json.dumps(all_states) if all_states else ""
        super().__init__(
            store_data=store,
            user_data_json=user_data_json,
            update_interval=update_interval,
        )
        if all_states:
            logger.info("Loaded user_data for %d user(s) from Supabase", len(all_states))

    async def update_user_data(self, user_id: int, data: dict) -> None:
        await super().update_user_data(user_id, data)
        try:
            await asyncio.to_thread(save_user_data_state, str(user_id), data)
        except Exception as exc:
            logger.warning("Failed to persist user_data for %s: %s", user_id, exc)

    async def drop_user_data(self, user_id: int) -> None:
        await super().drop_user_data(user_id)
        try:
            await asyncio.to_thread(delete_user_data_state, str(user_id))
        except Exception as exc:
            logger.warning("Failed to delete user_data for %s: %s", user_id, exc)
