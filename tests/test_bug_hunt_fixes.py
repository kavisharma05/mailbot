"""Regression tests for full-codebase bug hunt fixes."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.db as db_module
from bot.handlers import _category_for_chat
from services.db import (
    _PROCESSED_CACHE_MAX_ENTRIES,
    _processed_cache,
    cache_email_processed,
    claim_digest,
    is_email_processed_cached,
)
from services.watcher import run_gmail_watcher


def _settings(**overrides):
    base = {
        "telegram_chat_deadlines": "-1001",
        "telegram_chat_placements": "-1002",
        "telegram_chat_certification": "",
        "telegram_chat_admin": "-1004",
        "telegram_chat_other": "-1005",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_category_for_chat_placements_when_certification_blank():
    with patch("bot.handlers.get_settings", return_value=_settings()):
        assert _category_for_chat("-1002") == "placement"


def test_category_for_chat_certification_when_dedicated_chat_set():
    with patch(
        "bot.handlers.get_settings",
        return_value=_settings(telegram_chat_certification="-1003"),
    ):
        assert _category_for_chat("-1002") == "placement"
        assert _category_for_chat("-1003") == "certification"


def test_category_for_chat_skips_blank_ids():
    with patch(
        "bot.handlers.get_settings",
        return_value=_settings(
            telegram_chat_deadlines="",
            telegram_chat_placements="-1002",
            telegram_chat_admin="",
            telegram_chat_other="",
        ),
    ):
        assert _category_for_chat("") is None
        assert _category_for_chat("-1002") == "placement"


def test_claim_digest_in_memory_only_one_winner():
    day = date(2026, 7, 23)
    original_state = dict(db_module._digest_state_fallback)

    with (
        patch.object(db_module, "_digest_state_table_missing", True),
        patch.object(db_module, "_delivery_claims_table_missing", True),
    ):
        db_module._digest_state_fallback["last_morning"] = None
        db_module._digest_state_fallback["last_evening"] = None
        db_module._digest_leases_fallback.clear()
        try:
            assert claim_digest("morning", day) is True
            assert claim_digest("morning", day) is False
        finally:
            db_module._digest_state_fallback.clear()
            db_module._digest_state_fallback.update(original_state)
            db_module._digest_leases_fallback.clear()


def test_processed_cache_evicts_expired_and_caps_size():
    _processed_cache.clear()
    import time

    now = time.time()
    for i in range(_PROCESSED_CACHE_MAX_ENTRIES + 50):
        _processed_cache[f"expired-{i}"] = now - 10
    cache_email_processed("fresh")
    assert len(_processed_cache) <= _PROCESSED_CACHE_MAX_ENTRIES
    assert "fresh" in _processed_cache

    with patch("services.db.is_email_processed", return_value=False):
        assert is_email_processed_cached("never-seen") is False


def test_watcher_defers_history_advance_on_transient_failure():
    state = {"last_history_id": "1000"}
    update_calls: list[dict] = []

    def fake_update_watch_state(**kwargs):
        update_calls.append(kwargs)

    async def fake_process(_message_id, _bot, **kwargs):
        return {"processed": 0, "alerts": 0, "transient": True}

    async def _run():
        with (
            patch("services.watcher.get_watch_state", return_value=state),
            patch(
                "services.watcher._collect_message_ids",
                new=AsyncMock(return_value=(["msg-1"], "1005", False)),
            ),
            patch("services.watcher.is_email_processed_cached", return_value=False),
            patch("services.watcher._process_message", side_effect=fake_process),
            patch("services.watcher.update_watch_state", side_effect=fake_update_watch_state),
        ):
            result = await run_gmail_watcher(bot=object())

        assert result["failed_retries"] == 1
        assert result["history_id"] == "1000"
        assert len(update_calls) == 1
        assert update_calls[0].get("last_history_id") is None

    asyncio.run(_run())
