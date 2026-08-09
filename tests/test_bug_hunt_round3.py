"""Regression tests for polish round 3: digest, NL, alerts, reminders."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import services.db as db_module
from ai.assistant import _extract_person_query, _filter_emails_for_person
from ai.intent import _is_specific_deadline_question, classify_message
from bot.handlers import (
    COMPOSE_CONFIRM,
    _continue_compose_flow,
    _get_compose,
    _looks_like_compose_breakout,
)
from services.digest import _tomorrow_classes_text, run_evening_digest, run_morning_digest
from services.formatting import is_priority_item, priority_prefix
from services.watcher import format_mail_alert, run_due_reminders


def test_tomorrow_classes_text_is_defined():
    assert asyncio.iscoroutinefunction(_tomorrow_classes_text)


FROZEN_NOW = datetime(2026, 7, 23, 8, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))


class _FrozenDateTime(datetime):
    """datetime stand-in with a pinned now() so digest tests are date-independent."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW.astimezone(tz) if tz else FROZEN_NOW


def test_digest_marks_only_after_successful_send():
    db_module._digest_state_table_missing = True
    db_module._digest_state_fallback["last_morning"] = None
    db_module._digest_leases_fallback.clear()
    day = date(2026, 7, 23)
    bot = MagicMock()

    async def _run():
        with (
            patch("services.digest.datetime", _FrozenDateTime),
            patch("services.digest.digest_already_sent_today", return_value=False),
            patch("services.digest.acquire_digest_lease", return_value=True),
            patch("services.digest.classes_command_text", return_value=""),
            patch("services.digest.get_assignments_due_on", return_value=[]),
            patch("services.digest.get_recent_assignments", return_value=[]),
            patch("services.digest._send_owner_digest", new=AsyncMock(return_value=False)),
            patch("services.digest.release_digest_lease") as release,
            patch("services.digest.mark_digest_sent") as mark,
        ):
            result = await run_morning_digest(bot, dry_run=False)
            mark.assert_not_called()
            release.assert_called_once_with("morning", day)
            assert result["sent"] == []

        db_module._digest_state_fallback["last_morning"] = None
        db_module._digest_leases_fallback.clear()
        with (
            patch("services.digest.datetime", _FrozenDateTime),
            patch("services.digest.digest_already_sent_today", return_value=False),
            patch("services.digest.acquire_digest_lease", return_value=True),
            patch("services.digest.classes_command_text", return_value=""),
            patch("services.digest.get_assignments_due_on", return_value=[]),
            patch("services.digest.get_recent_assignments", return_value=[]),
            patch("services.digest._send_owner_digest", new=AsyncMock(return_value=True)),
            patch("services.digest.mark_digest_sent") as mark,
        ):
            result = await run_morning_digest(bot, dry_run=False)
            mark.assert_called_once_with("morning", day)
            assert result["sent"] == ["owner"]

    asyncio.run(_run())


def test_digest_send_failure_allows_retry():
    db_module._digest_state_table_missing = True
    db_module._digest_state_fallback["last_morning"] = None
    db_module._digest_leases_fallback.clear()
    day = date(2026, 7, 23)
    bot = MagicMock()

    async def _run():
        with (
            patch("services.digest.datetime", _FrozenDateTime),
            patch("services.digest.digest_already_sent_today", return_value=False),
            patch("services.digest.acquire_digest_lease", return_value=True),
            patch("services.digest.classes_command_text", return_value=""),
            patch("services.digest.get_assignments_due_on", return_value=[]),
            patch("services.digest.get_recent_assignments", return_value=[]),
            patch("services.digest._send_owner_digest", new=AsyncMock(return_value=False)),
            patch("services.digest.release_digest_lease"),
            patch("services.digest.mark_digest_sent"),
        ):
            first = await run_morning_digest(bot, dry_run=False)
            assert first.get("skipped") is None

        db_module._digest_leases_fallback.clear()
        with (
            patch("services.digest.datetime", _FrozenDateTime),
            patch("services.digest.digest_already_sent_today", return_value=False),
            patch("services.digest.acquire_digest_lease", return_value=True),
            patch("services.digest.classes_command_text", return_value=""),
            patch("services.digest.get_assignments_due_on", return_value=[]),
            patch("services.digest.get_recent_assignments", return_value=[]),
            patch("services.digest._send_owner_digest", new=AsyncMock(return_value=True)),
            patch("services.digest.mark_digest_sent") as mark,
        ):
            second = await run_morning_digest(bot, dry_run=False)
            mark.assert_called_once_with("morning", day)
            assert second["sent"] == ["owner"]

    asyncio.run(_run())


def test_evening_digest_calls_tomorrow_classes_text():
    bot = MagicMock()

    async def _run():
        with (
            patch("services.digest.digest_already_sent_today", return_value=False),
            patch("services.digest.acquire_digest_lease", return_value=True),
            patch("services.digest._tomorrow_classes_text", new=AsyncMock(return_value="")),
            patch("services.digest.get_assignments_due_on", return_value=[]),
            patch("services.digest.get_assignments_due_between", return_value=[]),
            patch("services.digest._send_owner_digest", new=AsyncMock(return_value=True)),
            patch("services.digest.mark_digest_sent"),
        ):
            await run_evening_digest(bot, dry_run=False)

    asyncio.run(_run())


def test_compose_confirm_replies_on_free_text():
    user_data: dict = {
        "compose": {
            "step": COMPOSE_CONFIRM,
            "recipient": "a@b.com",
            "instructions": "hello",
            "draft": {"subject": "Hi", "body": "Body"},
        }
    }
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        await _continue_compose_flow(update, context, "maybe later?")
        update.message.reply_text.assert_called_once()
        assert _get_compose(user_data) is not None

    asyncio.run(_run())


def test_compose_confirm_cancel_clears():
    user_data: dict = {
        "compose": {
            "step": COMPOSE_CONFIRM,
            "recipient": "a@b.com",
            "draft": {"subject": "Hi", "body": "Body"},
        }
    }
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        await _continue_compose_flow(update, context, "cancel")
        assert _get_compose(user_data) is None

    asyncio.run(_run())


def test_cv_deadline_not_due_list_intent():
    result = classify_message("when is the CV deadline?")
    assert result["intent"] != "due_list"


def test_due_today_still_due_list_intent():
    result = classify_message("what's due today?")
    assert result["intent"] == "due_list"


def test_specific_deadline_question_routing():
    assert _is_specific_deadline_question("when is the CV deadline?")
    assert not _is_specific_deadline_question("what's due today?")


def test_person_extract_case_insensitive():
    assert _extract_person_query("what did saleena say about the project")


def test_person_filter_no_fallback_to_all_mail():
    emails = [
        {"sender": "Other Person <other@x.com>", "subject": "Hi"},
        {"sender": "Someone Else <else@x.com>", "subject": "Bye"},
    ]
    filtered = _filter_emails_for_person(emails, "Santhi")
    assert filtered == []


def test_priority_only_placement_cert_or_near_due():
    near = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    assert is_priority_item("placement", task="Upload CV")
    assert is_priority_item("certification", task="NSE")
    assert is_priority_item("deadline", task="Submit", deadline_date=near)
    assert not is_priority_item("deadline", task="Read Brady case", summary="reading")
    assert not is_priority_item("deadline", task="Submit", deadline_date=far)
    assert not is_priority_item("other", task="Newsletter")


def test_unified_alert_format_order():
    text = format_mail_alert(
        header="📩 <b>New mail</b>",
        sender="Prof <a@b.com>",
        subject="Assignment",
        category="deadline",
        task="Submit report",
        deadline_date=None,
        deadline_text="Friday",
        summary="Please submit.",
    )
    assert text.index("From:") < text.index("Subject:")
    assert text.index("Subject:") < text.index("Task:")
    assert text.index("Task:") < text.index("Due:")
    assert text.index("Due:") < text.index("Category:")
    assert "Reminder" not in text


def test_reminder_header_not_fake_subject():
    from services.watcher import format_alert_message

    text = format_alert_message(
        category="deadline",
        course_or_topic=None,
        task="Submit CV",
        deadline_date=None,
        deadline_text="tomorrow",
        summary="Don't forget",
        sender="admin@x.com",
        subject="Actual subject line",
        header="⏰ <b>Reminder</b>",
    )
    assert text.startswith("⏰ <b>Reminder</b>") or "⏰ <b>Reminder</b>" in text.split("\n")[0:2]
    assert "Subject:</b> Actual subject line" in text
    assert "Subject:</b> Reminder" not in text


def test_reminder_clear_skipped_on_failed_send():
    bot = MagicMock()
    item = {
        "id": "abc",
        "category": "deadline",
        "task": "Submit",
        "deadline_date": None,
        "deadline_text": "tomorrow",
        "summary": "Reminder",
    }

    async def _run():
        with (
            patch("services.db.get_assignments_due_reminder", return_value=[item]),
            patch(
                "services.watcher.get_settings",
                return_value=SimpleNamespace(telegram_owner_chat_id="123"),
            ),
            patch("services.watcher.send_actionable_alert", new=AsyncMock(return_value=None)),
            patch("services.watcher.clear_assignment_reminder") as clear,
        ):
            result = await run_due_reminders(bot)
            clear.assert_not_called()
            assert result["reminders_sent"] == 0

    asyncio.run(_run())


def test_reminder_clears_after_successful_send():
    bot = MagicMock()
    item = {
        "id": "abc",
        "category": "deadline",
        "task": "Submit",
        "deadline_date": None,
        "deadline_text": "tomorrow",
        "summary": "Reminder",
    }

    async def _run():
        with (
            patch("services.db.get_assignments_due_reminder", return_value=[item]),
            patch(
                "services.watcher.get_settings",
                return_value=SimpleNamespace(telegram_owner_chat_id="123"),
            ),
            patch("services.watcher.send_actionable_alert", new=AsyncMock(return_value=42)),
            patch("services.watcher.clear_assignment_reminder") as clear,
        ):
            result = await run_due_reminders(bot)
            clear.assert_called_once_with("abc")
            assert result["reminders_sent"] == 1

    asyncio.run(_run())


def test_compose_breakout_detects_due_question():
    assert _looks_like_compose_breakout("what's due today?")


def test_compose_breakout_does_not_clear_short_recipient_name():
    """A short recipient answer (e.g. 'saleena') must not be treated as a
    compose breakout even if the classifier defaults it to mailbox_qa."""
    assert not _looks_like_compose_breakout("saleena")
    assert not _looks_like_compose_breakout("saleena mam")
    assert not _looks_like_compose_breakout("Dr. Santhi")
    # But a real question still breaks out
    assert _looks_like_compose_breakout("what's due today?")


def test_send_flow_content_then_ask_recipient_not_misrouted():
    """End-to-end: /send with no args asks for content; user replies with
    text containing 'from'/'class'; the content is captured and the bot
    asks 'Who should I send it to?' instead of misrouting or clearing."""
    from bot.handlers import (
        COMPOSE_NEED_CONTENT,
        COMPOSE_NEED_RECIPIENT,
        _begin_compose_flow,
        _continue_compose_flow,
        _get_compose,
    )

    user_data: dict = {}
    update = MagicMock()
    update.message.text = "/send"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data

    async def _run():
        # Step 1: /send with no args -> asks for content
        await _begin_compose_flow(update, context, recipient=None, content=None)
        compose = _get_compose(user_data)
        assert compose is not None
        assert compose["step"] == COMPOSE_NEED_CONTENT
        assert compose["instructions"] is None

        # Step 2: user provides content that previously triggered misroute
        content_text = "Please share notes from today's class"
        await _continue_compose_flow(update, context, content_text)
        compose = _get_compose(user_data)
        assert compose is not None
        # Content was captured, not treated as recipient
        assert compose["instructions"] == content_text
        # No recipient yet -> now asking for recipient
        assert compose["step"] == COMPOSE_NEED_RECIPIENT

    asyncio.run(_run())


def test_chat_message_content_step_routes_to_continue_not_breakout():
    """chat_message must route content-step text directly to
    _continue_compose_flow without running breakout detection."""
    from bot.handlers import COMPOSE_NEED_CONTENT, chat_message

    user_data: dict = {
        "compose": {
            "step": COMPOSE_NEED_CONTENT,
            "recipient": None,
            "instructions": None,
            "draft": None,
        }
    }
    update = MagicMock()
    update.effective_user = MagicMock(id=1)
    update.effective_chat = MagicMock(id=1, type="private")
    update.message.text = "Please share notes from today's class"
    update.message.reply_text = AsyncMock()
    update.message.entities = []
    context = MagicMock()
    context.user_data = user_data
    context.bot = MagicMock(id=2, username="bot")

    async def _run():
        with patch("bot.handlers._is_owner", return_value=True):
            await chat_message(update, context)
        # Compose must still exist and have content captured
        compose = _get_compose(user_data)
        assert compose is not None
        assert compose["instructions"] == "Please share notes from today's class"

    asyncio.run(_run())
