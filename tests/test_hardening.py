"""Tests for comprehensive hardening: formatting, delivery, due list, OAuth, auth."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.db as db_module
from bot.handlers import _format_due_list, _is_owner, action_button
from services.formatting import (
    escape_html,
    format_due_list,
    format_morning_digest,
    sort_assignments,
)
from services.oauth_state import create_oauth_state, verify_oauth_state
from services.telegram_util import chunk_text, deliver_message
from ai.assistant import _sanitize_gmail_query


def test_escape_html_quotes_and_brackets():
    assert escape_html('Tom & "Jerry" <case>') == "Tom &amp; &quot;Jerry&quot; &lt;case&gt;"


def test_sort_assignments_priority_first():
    near = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    items = [
        {"id": "1", "category": "other", "task": "Newsletter", "deadline_date": None},
        {"id": "2", "category": "placement", "task": "Upload CV", "deadline_date": near},
    ]
    ordered = sort_assignments(items)
    assert ordered[0]["id"] == "2"


def test_due_list_numbering_matches_sorted_ids():
    near = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    items = [
        {"id": "a", "category": "other", "task": "Later", "deadline_date": far, "course_or_topic": "X"},
        {"id": "b", "category": "placement", "task": "CV", "deadline_date": near, "course_or_topic": "Y"},
    ]
    text, ids = format_due_list(items, None)
    assert "1." in text
    assert "2." in text
    assert ids[0] == "b"
    assert "Showing 2 of 2" not in text


def test_due_list_truncation_footer():
    items = [
        {
            "id": str(i),
            "category": "deadline",
            "task": f"Task {i}",
            "deadline_date": None,
            "course_or_topic": "Course",
        }
        for i in range(25)
    ]
    text, ids = format_due_list(items, "deadline")
    assert len(ids) == 20
    assert "…and 5 more." in text


def test_handlers_due_list_wrapper():
    items = [{"id": "x", "category": "deadline", "task": "Submit", "deadline_date": None}]
    text, ids = _format_due_list(items, "deadline")
    assert ids == ["x"]
    assert "Deadline" in text


def test_morning_digest_deduplicates_priority_and_due():
    near = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
    item = {
        "id": "same",
        "category": "placement",
        "task": "Upload CV",
        "deadline_date": near,
        "course_or_topic": "Placement",
    }
    text = format_morning_digest(
        today=datetime.now(),
        classes_text="",
        today_items=[item],
        recent_items=[item],
    )
    assert text.count("Upload CV") == 1


def test_chunk_text_splits_long_plain_answer():
    body = "\n".join(f"Line {i}: {'x' * 200}" for i in range(30))
    chunks = chunk_text(body, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_deliver_message_retries_transient_failure():
    attempts = {"count": 0}

    async def send_fn(text: str, *, parse_mode: str | None = None):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return SimpleNamespace(message_id=42)

    async def _run():
        result = await deliver_message(send_fn, "hello", parse_mode=None, allow_plain_fallback=False)
        assert result.ok is True
        assert result.first_message_id == 42
        assert attempts["count"] == 2

    asyncio.run(_run())


def test_oauth_state_roundtrip():
    with patch("services.oauth_state.get_settings") as mock_settings:
        mock_settings.return_value.task_secret = "test-secret"
        state = create_oauth_state()
        assert verify_oauth_state(state)


def test_oauth_state_rejects_tampered():
    with patch("services.oauth_state.get_settings") as mock_settings:
        mock_settings.return_value.task_secret = "test-secret"
        state = create_oauth_state()
        assert not verify_oauth_state(state + "x")


def test_sanitize_gmail_query_blocks_newlines_and_delete():
    assert _sanitize_gmail_query("from:prof newer_than:30d") == "from:prof newer_than:30d"
    assert _sanitize_gmail_query("delete everything") is None
    assert _sanitize_gmail_query("line1\nline2") is None


def test_is_email_processed_unknown_on_db_failure():
    with patch("services.db._is_email_processed_db", side_effect=RuntimeError("db down")):
        assert db_module.is_email_processed("msg-1") is None


def test_is_email_processed_cached_unknown():
    db_module._processed_cache.clear()
    with patch("services.db.is_email_processed", return_value=None):
        assert db_module.is_email_processed_cached("msg-2") is None


def test_action_button_requires_owner():
    update = MagicMock()
    update.callback_query = MagicMock()
    update.callback_query.data = "act:done:abc"
    update.callback_query.answer = AsyncMock()
    update.effective_user = SimpleNamespace(id=999)
    context = MagicMock()

    settings = SimpleNamespace(
        telegram_owner_chat_id="123",
    )
    settings.owner_user_id = lambda: "123"

    with patch("bot.handlers.get_settings", return_value=settings):
        asyncio.run(action_button(update, context))
    update.callback_query.answer.assert_called_once()
