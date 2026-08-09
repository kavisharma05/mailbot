"""Reply-to-sender and snooze-tomorrow alert actions, plus alert context storage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import (
    COMPOSE_NEED_CONTENT,
    _draft_and_show_compose,
    _get_compose,
    action_button,
)
from services.alert_actions import build_action_keyboard, parse_action_callback
from services.timeutil import IST, now_ist
from services.watcher import _assignment_payload


def _settings(**overrides):
    base = {"remind_again_hours": 4}
    base.update(overrides)
    return SimpleNamespace(**base)


def _callback_update(data: str) -> MagicMock:
    update = MagicMock()
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.message.text = "alert text"
    query.message.caption = None
    query.message.reply_markup = None
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update


def test_keyboard_reply_button_toggle():
    default = [b.callback_data for row in build_action_keyboard("abc").inline_keyboard for b in row]
    assert not any(d.startswith("act:reply") for d in default)

    with_reply = [
        b.callback_data
        for row in build_action_keyboard("abc", show_reply=True).inline_keyboard
        for b in row
    ]
    assert "act:reply:abc" in with_reply


def test_keyboard_has_snooze_tomorrow():
    data = [b.callback_data for row in build_action_keyboard("abc").inline_keyboard for b in row]
    assert "act:snooze:abc:tomorrow" in data


def test_parse_action_callback_reply_and_snooze_extra():
    assert parse_action_callback("act:reply:abc") == ("reply", "abc", "")
    assert parse_action_callback("act:snooze:abc:tomorrow") == ("snooze", "abc", "tomorrow")
    assert parse_action_callback("act:bogus:abc") is None


def test_assignment_payload_stores_sender_and_subject():
    result = {"sender": "Prof Saleena <saleena@x.edu>", "task": "Submit", "summary": "s"}
    details = {
        "thread_id": "t1",
        "message_id": "m1",
        "sender": "Prof Saleena <saleena@x.edu>",
        "subject": "Brady case",
    }
    payload = _assignment_payload(result, details, "deadline", "high")
    assert payload["sender"] == "Prof Saleena <saleena@x.edu>"
    assert payload["subject"] == "Brady case"


def test_assignment_payload_falls_back_to_details_sender():
    result = {"task": "Submit", "summary": "s"}
    details = {"thread_id": "t1", "message_id": "m1", "sender": "Admin <a@x.edu>", "subject": "Fees"}
    payload = _assignment_payload(result, details, "admin", "medium")
    assert payload["sender"] == "Admin <a@x.edu>"


def test_reply_button_seeds_compose_with_sender_and_thread():
    item = {
        "id": "abc",
        "sender": "Prof Saleena <saleena@x.edu>",
        "subject": "Brady case discussion",
        "gmail_thread_id": "thread-123",
    }
    user_data: dict = {}
    update = _callback_update("act:reply:abc")
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch("bot.handlers.get_settings", return_value=_settings()),
            patch("bot.handlers.get_assignment_by_id", return_value=item),
        ):
            await action_button(update, context)

        compose = _get_compose(user_data)
        assert compose is not None
        assert compose["step"] == COMPOSE_NEED_CONTENT
        assert compose["recipient"] == "saleena@x.edu"
        assert compose["recipient_name"] == "Prof Saleena"
        assert compose["reply_subject"] == "Brady case discussion"
        assert compose["reply_thread_id"] == "thread-123"
        update.callback_query.message.reply_text.assert_called_once()

    asyncio.run(_run())


def test_reply_button_without_sender_shows_alert():
    item = {"id": "abc", "sender": None, "subject": "x"}
    user_data: dict = {}
    update = _callback_update("act:reply:abc")
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch("bot.handlers.get_settings", return_value=_settings()),
            patch("bot.handlers.get_assignment_by_id", return_value=item),
        ):
            await action_button(update, context)

        assert _get_compose(user_data) is None
        update.callback_query.answer.assert_called_once()
        assert update.callback_query.answer.call_args.kwargs.get("show_alert") is True

    asyncio.run(_run())


def test_snooze_tomorrow_targets_9am_ist():
    captured: dict = {}

    def fake_schedule(assignment_id, remind_at):
        captured["assignment_id"] = assignment_id
        captured["remind_at"] = remind_at
        return {"id": assignment_id}

    update = _callback_update("act:snooze:abc:tomorrow")
    context = MagicMock()

    async def _run():
        with (
            patch("bot.handlers._is_owner", return_value=True),
            patch("bot.handlers.get_settings", return_value=_settings()),
            patch("bot.handlers.schedule_assignment_reminder", side_effect=fake_schedule),
        ):
            await action_button(update, context)

        remind_at = captured["remind_at"]
        ist = remind_at.astimezone(IST)
        today = now_ist().date()
        assert ist.date() == today + timedelta(days=1)
        assert (ist.hour, ist.minute) == (9, 0)

    asyncio.run(_run())


def test_reply_draft_overrides_subject_with_re_prefix():
    user_data: dict = {
        "compose": {
            "step": COMPOSE_NEED_CONTENT,
            "recipient": "prof@x.edu",
            "recipient_name": "Prof",
            "reply_subject": "Re: Brady case",
            "reply_thread_id": "thread-123",
            "instructions": "thank her and ask for a two-day extension",
            "draft": None,
        }
    }
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        with patch(
            "bot.handlers.compose_email",
            return_value={"subject": "Extension request", "body": "Body"},
        ) as comp:
            await _draft_and_show_compose(update, context)

        compose = _get_compose(user_data)
        assert compose is not None
        # No double "Re: Re:" and original subject preserved
        assert compose["draft"]["subject"] == "Re: Brady case"
        # Composer prompt carries the reply context
        prompt = comp.call_args.args[0]
        assert "Brady case" in prompt

    asyncio.run(_run())


def test_compose_without_reply_keeps_llm_subject():
    user_data: dict = {
        "compose": {
            "step": COMPOSE_NEED_CONTENT,
            "recipient": "prof@x.edu",
            "instructions": "ask about office hours",
            "draft": None,
        }
    }
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        with patch(
            "bot.handlers.compose_email",
            return_value={"subject": "Office hours", "body": "Body"},
        ):
            await _draft_and_show_compose(update, context)

        compose = _get_compose(user_data)
        assert compose is not None
        assert compose["draft"]["subject"] == "Office hours"

    asyncio.run(_run())
