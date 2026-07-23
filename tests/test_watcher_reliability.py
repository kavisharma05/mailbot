"""Regression tests for Gmail watcher reliability fixes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from services.contacts import backfill_contacts_from_inbox
from services.watcher import (
    CATCHUP_DAYS,
    _process_message,
    _recent_unprocessed_message_ids,
    run_gmail_watcher,
)


def test_list_history_includes_empty_label_ids():
    from gmail_api.client import list_history_message_ids

    service = MagicMock()
    service.users.return_value.history.return_value.list.return_value.execute.return_value = {
        "historyId": "2000",
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "msg-unlabeled"}},
                    {"message": {"id": "msg-inbox", "labelIds": ["INBOX"]}},
                    {"message": {"id": "msg-sent", "labelIds": ["SENT"]}},
                ]
            }
        ],
    }

    with patch("gmail_api.client.get_gmail_service", return_value=service):
        ids, latest, failed = list_history_message_ids("1000")

    assert failed is False
    assert latest == "2000"
    assert "msg-unlabeled" in ids
    assert "msg-inbox" in ids
    assert "msg-sent" not in ids


def test_list_history_failure_marks_fallback():
    from gmail_api.client import list_history_message_ids

    with (
        patch("gmail_api.client.get_gmail_service", side_effect=RuntimeError("history down")),
        patch(
            "gmail_api.client.list_recent_inbox_message_ids",
            return_value=["a", "b"],
        ),
    ):
        ids, latest, failed = list_history_message_ids("1000")

    assert failed is True
    assert latest is None
    assert ids == ["a", "b"]


def test_watcher_defers_history_advance_on_history_api_failure():
    state = {"last_history_id": "1000"}
    update_calls: list[dict] = []

    def fake_update_watch_state(**kwargs):
        update_calls.append(kwargs)

    async def fake_process(_message_id, _bot, **kwargs):
        return {"processed": 1, "alerts": 0}

    async def _run():
        with (
            patch("services.watcher.get_watch_state", return_value=state),
            patch(
                "services.watcher._collect_message_ids",
                new=AsyncMock(return_value=(["msg-1"], None, True)),
            ),
            patch("services.watcher.is_email_processed_cached", return_value=False),
            patch("services.watcher._process_message", side_effect=fake_process),
            patch("services.watcher.update_watch_state", side_effect=fake_update_watch_state),
            patch("services.watcher.get_profile_history_id", return_value="9999"),
        ):
            result = await run_gmail_watcher(bot=object())

        assert result["history_failed"] is True
        assert result["history_id"] == "1000"
        assert update_calls[0].get("last_history_id") is None

    asyncio.run(_run())


def test_recent_catchup_uses_inbox_spam_pagination():
    with patch(
        "services.watcher.list_inbox_message_ids_since",
        return_value=["m1", "m2"],
    ) as mock_list:
        ids = _recent_unprocessed_message_ids()

    assert ids == ["m1", "m2"]
    mock_list.assert_called_once_with(days=CATCHUP_DAYS, max_results=300)


def test_backfill_contacts_uses_light_sender_fetch():
    with (
        patch(
            "gmail_api.client.list_message_senders",
            return_value=[
                {"message_id": "1", "sender": "Saleena Khan <saleena@imtnag.ac.in>"},
            ],
        ) as mock_senders,
        patch("services.contacts.upsert_contact_from_sender") as mock_upsert,
    ):
        stats = backfill_contacts_from_inbox(days=30, max_messages=50)

    assert stats["error"] == 0
    assert stats["upserted"] == 1
    mock_senders.assert_called_once()
    mock_upsert.assert_called_once()


def test_process_message_defers_when_owner_alert_fails():
    async def _run():
        details = {
            "message_id": "msg-1",
            "thread_id": "t-1",
            "subject": "Test",
            "sender": "Academic Office <office@imtnag.ac.in>",
            "body": "Please report attendance issues",
            "received_at_iso": "2026-07-23T10:00:00+00:00",
            "attachments": [],
            "attachment_text": "",
        }
        classify_result = {
            "is_actionable": False,
            "confidence": "high",
            "category": "admin",
            "task": "Report attendance-related issues",
            "summary": "Use face recognition attendance system",
        }

        with (
            patch("services.watcher.get_message_details", return_value=details),
            patch("services.watcher.upsert_contact_from_sender"),
            patch("services.watcher.annotate_attachment_intents", return_value=[]),
            patch("services.watcher.classify_email", return_value=classify_result),
            patch("services.watcher.dequeue_pending_email"),
            patch("services.watcher.get_settings") as mock_settings,
            patch("services.watcher.is_trusted_schedule_sender", return_value=False),
            patch("services.watcher.send_owner_update", new=AsyncMock(return_value=False)),
            patch("services.watcher.enqueue_pending_email") as mock_enqueue,
            patch("services.watcher.mark_email_processed") as mock_mark,
        ):
            mock_settings.return_value.owner_dm_all_mail = True
            mock_settings.return_value.telegram_owner_chat_id = "123"

            outcome = await _process_message("msg-1", bot=object())

        assert outcome["transient"] is True
        assert outcome["processed"] == 0
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.args[0] == "msg-1"
        assert mock_enqueue.call_args.args[1] == "alert_delivery_failed"
        mock_mark.assert_not_called()

    asyncio.run(_run())


def test_init_path_defers_history_on_transient_failure():
    update_calls: list[dict] = []

    def fake_update_watch_state(**kwargs):
        update_calls.append(kwargs)

    async def fake_process(_message_id, _bot, **kwargs):
        return {"processed": 0, "alerts": 0, "transient": True}

    async def _run():
        with (
            patch("services.watcher.get_watch_state", return_value=None),
            patch(
                "services.watcher._collect_message_ids",
                new=AsyncMock(return_value=(["msg-1"], "1005", False)),
            ),
            patch("services.watcher.is_email_processed_cached", return_value=False),
            patch("services.watcher._process_message", side_effect=fake_process),
            patch("services.watcher.update_watch_state", side_effect=fake_update_watch_state),
            patch("services.watcher.get_profile_history_id", return_value="1005"),
        ):
            result = await run_gmail_watcher(bot=object())

        assert result["failed_retries"] == 1
        assert result.get("history_id") is None
        assert update_calls[0].get("last_history_id") is None

    asyncio.run(_run())


def test_dead_letter_message_marked_processed_without_reprocessing():
    async def _run():
        with (
            patch(
                "services.watcher.get_pending_email",
                return_value={"dead_letter": True, "last_error": "alert_delivery_failed"},
            ),
            patch("services.watcher.get_message_details") as mock_details,
            patch("services.watcher.mark_email_processed", return_value=True) as mock_mark,
            patch("services.watcher.dequeue_pending_email") as mock_dequeue,
        ):
            outcome = await _process_message("msg-dead", bot=object())

        assert outcome["processed"] == 1
        mock_details.assert_not_called()
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs.get("delivery_failed") is True
        mock_dequeue.assert_called_once_with("msg-dead")

    asyncio.run(_run())


def test_alert_delivery_retry_reuses_cached_classification():
    async def _run():
        cached = {
            "is_actionable": False,
            "confidence": "high",
            "category": "admin",
            "task": "Task",
            "summary": "Summary",
        }
        details = {
            "message_id": "msg-1",
            "thread_id": "t-1",
            "subject": "Test",
            "sender": "Office <office@imtnag.ac.in>",
            "body": "Body",
            "received_at_iso": "2026-07-23T10:00:00+00:00",
            "attachments": [],
            "attachment_text": "",
        }

        with (
            patch(
                "services.watcher.get_pending_email",
                return_value={
                    "last_error": "alert_delivery_failed",
                    "classified_result": cached,
                    "already_alerted": False,
                    "dead_letter": False,
                },
            ),
            patch("services.watcher.get_message_details", return_value=details),
            patch("services.watcher.upsert_contact_from_sender"),
            patch("services.watcher.annotate_attachment_intents", return_value=[]),
            patch("services.watcher.classify_email") as mock_classify,
            patch("services.watcher.dequeue_pending_email"),
            patch("services.watcher.get_settings") as mock_settings,
            patch("services.watcher.is_trusted_schedule_sender", return_value=False),
            patch("services.watcher.send_owner_update", new=AsyncMock(return_value=True)),
            patch("services.watcher.mark_email_processed", return_value=True),
        ):
            mock_settings.return_value.owner_dm_all_mail = True
            mock_settings.return_value.telegram_owner_chat_id = "123"
            outcome = await _process_message("msg-1", bot=object())

        assert outcome["processed"] == 1
        mock_classify.assert_not_called()

    asyncio.run(_run())


def test_actionable_insert_failure_is_transient():
    async def _run():
        details = {
            "message_id": "msg-1",
            "thread_id": "t-1",
            "subject": "Test",
            "sender": "Office <office@imtnag.ac.in>",
            "body": "Body",
            "received_at_iso": "2026-07-23T10:00:00+00:00",
            "attachments": [],
            "attachment_text": "",
        }
        classify_result = {
            "is_actionable": True,
            "confidence": "high",
            "category": "deadline",
            "task": "Submit report",
            "summary": "Due soon",
        }

        with (
            patch("services.watcher.get_pending_email", return_value=None),
            patch("services.watcher.get_message_details", return_value=details),
            patch("services.watcher.upsert_contact_from_sender"),
            patch("services.watcher.annotate_attachment_intents", return_value=[]),
            patch("services.watcher.classify_email", return_value=classify_result),
            patch("services.watcher.dequeue_pending_email"),
            patch("services.watcher.get_settings") as mock_settings,
            patch("services.watcher.is_trusted_schedule_sender", return_value=False),
            patch("services.watcher.insert_assignment", return_value=None),
            patch("services.watcher.enqueue_pending_email") as mock_enqueue,
            patch("services.watcher.mark_email_processed") as mock_mark,
        ):
            mock_settings.return_value.owner_dm_all_mail = False
            mock_settings.return_value.telegram_owner_chat_id = "123"
            outcome = await _process_message("msg-1", bot=object())

        assert outcome["transient"] is True
        assert mock_enqueue.call_args.args[1] == "assignment_insert_failed"
        mock_mark.assert_not_called()

    asyncio.run(_run())


def test_mark_failed_retry_skips_realert():
    async def _run():
        cached = {
            "is_actionable": False,
            "confidence": "high",
            "category": "admin",
            "task": "Task",
            "summary": "Summary",
        }
        details = {
            "message_id": "msg-1",
            "thread_id": "t-1",
            "subject": "Test",
            "sender": "Office <office@imtnag.ac.in>",
            "body": "Body",
            "received_at_iso": "2026-07-23T10:00:00+00:00",
            "attachments": [],
            "attachment_text": "",
        }

        with (
            patch(
                "services.watcher.get_pending_email",
                return_value={
                    "last_error": "mark_failed",
                    "classified_result": cached,
                    "already_alerted": True,
                    "dead_letter": False,
                },
            ),
            patch("services.watcher.get_message_details", return_value=details),
            patch("services.watcher.upsert_contact_from_sender"),
            patch("services.watcher.annotate_attachment_intents", return_value=[]),
            patch("services.watcher.classify_email") as mock_classify,
            patch("services.watcher.dequeue_pending_email"),
            patch("services.watcher.get_settings") as mock_settings,
            patch("services.watcher.is_trusted_schedule_sender", return_value=False),
            patch("services.watcher.send_owner_update", new=AsyncMock(return_value=True)) as mock_send,
            patch("services.watcher.mark_email_processed", return_value=True),
        ):
            mock_settings.return_value.owner_dm_all_mail = True
            mock_settings.return_value.telegram_owner_chat_id = "123"
            outcome = await _process_message("msg-1", bot=object())

        assert outcome["processed"] == 1
        mock_classify.assert_not_called()
        mock_send.assert_not_called()

    asyncio.run(_run())


def test_status_shows_stale_polling_warning():
    from bot.handlers import status_command

    stale = datetime.now(timezone.utc) - timedelta(minutes=30)

    class FakeMessage:
        def __init__(self):
            self.text = ""

        async def reply_text(self, text, **kwargs):
            self.text = text
            return self

    update = MagicMock()
    update.message = FakeMessage()
    update.effective_chat = MagicMock()
    context = MagicMock()

    with (
        patch("bot.handlers._is_owner", return_value=True),
        patch("bot.handlers.get_watch_state", return_value={"last_checked_at": stale.isoformat()}),
        patch("bot.handlers.get_digest_state", return_value={}),
        patch("bot.handlers.count_pending_scheduled_reminders", return_value=0),
        patch("bot.handlers.count_pending_emails", return_value=2),
        patch("bot.handlers.count_dead_letter_emails", return_value=1),
        patch("bot.handlers.reply_long", new=AsyncMock()) as mock_reply,
    ):
        asyncio.run(status_command(update, context))

    body = mock_reply.call_args.args[1]
    assert "Automatic polling looks stale" in body
    assert "Pending email retries" in body
    assert "Dead-letter emails" in body


def test_process_message_skips_own_sent_mail():
    async def _run():
        details = {
            "message_id": "msg-sent",
            "thread_id": "t-1",
            "subject": "Re: homework",
            "sender": "Me <me@example.com>",
            "body": "Thanks!",
            "received_at_iso": "2026-07-23T10:00:00+00:00",
            "attachments": [],
            "attachment_text": "",
            "label_ids": ["SENT"],
        }

        with (
            patch("services.watcher.get_pending_email", return_value=None),
            patch("services.watcher.get_message_details", return_value=details),
            patch("services.watcher.get_settings") as mock_settings,
            patch("services.watcher.dequeue_pending_email") as mock_dequeue,
            patch("services.watcher.mark_email_processed") as mock_mark,
            patch("services.watcher.send_owner_update", new=AsyncMock()) as mock_owner,
            patch("services.watcher.send_category_alert", new=AsyncMock()) as mock_category,
            patch("services.watcher.classify_email") as mock_classify,
        ):
            mock_settings.return_value.gmail_user = "me@example.com"
            mock_settings.return_value.owner_dm_all_mail = True

            outcome = await _process_message("msg-sent", bot=object())

        assert outcome == {"processed": 0, "alerts": 0}
        mock_dequeue.assert_called_once_with("msg-sent")
        mock_mark.assert_called_once_with("msg-sent", False, "high", None)
        mock_classify.assert_not_called()
        mock_owner.assert_not_called()
        mock_category.assert_not_called()

    asyncio.run(_run())
