import logging
from datetime import datetime, timezone

from ai.deadline import classify_email
from config import get_settings
from gmail_api.client import (
    get_message_details,
    get_profile_history_id,
    list_history_message_ids,
)
from services.db import (
    get_watch_state,
    insert_assignment,
    is_email_processed,
    log_low_confidence,
    mark_email_processed,
    update_watch_state,
)

logger = logging.getLogger(__name__)


async def send_telegram_alert(summary: str, bot) -> None:
    settings = get_settings()
    await bot.send_message(chat_id=settings.telegram_owner_chat_id, text=f"Deadline alert: {summary}")


def _parse_deadline_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return None


async def run_gmail_watcher(bot) -> dict:
    """Poll Gmail for new emails, classify deadlines, send alerts."""
    state = get_watch_state()
    now = datetime.now(timezone.utc)

    if not state or not state.get("last_history_id"):
        history_id = get_profile_history_id()
        update_watch_state(last_history_id=history_id, last_checked_at=now)
        logger.info("Initialized watch state at historyId=%s", history_id)
        return {"status": "initialized", "history_id": history_id, "processed": 0, "alerts": 0}

    start_id = state["last_history_id"]
    message_ids, latest_history_id = list_history_message_ids(start_id)

    processed = 0
    alerts = 0

    for message_id in message_ids:
        if is_email_processed(message_id):
            continue

        details = get_message_details(message_id)
        result = classify_email(details["subject"], details["body"], details["sender"])

        if result is None:
            log_low_confidence(
                "deadline_classifier",
                {"message_id": message_id, "subject": details["subject"]},
            )
            mark_email_processed(message_id, None, None)
            processed += 1
            continue

        is_actionable = result.get("is_actionable", False)
        confidence = result.get("confidence", "low")
        assignment_id = None

        if is_actionable and confidence in ("high", "medium"):
            assignment = insert_assignment(
                {
                    "source": "email",
                    "course_or_topic": result.get("course_or_topic"),
                    "task": result.get("task"),
                    "deadline_text": result.get("deadline_text"),
                    "deadline_date": _parse_deadline_date(result.get("deadline_date")),
                    "gmail_thread_id": details["thread_id"],
                    "gmail_message_id": details["message_id"],
                    "summary": result.get("summary"),
                    "confidence": confidence,
                    "status": "open",
                }
            )
            assignment_id = assignment["id"]

            summary = result.get("summary")
            if summary:
                await send_telegram_alert(summary, bot)
                alerts += 1
        elif is_actionable or confidence == "low":
            log_low_confidence(
                "deadline_classifier",
                {
                    "message_id": message_id,
                    "subject": details["subject"],
                    "result": result,
                },
            )

        mark_email_processed(message_id, is_actionable, confidence, assignment_id)
        processed += 1

    new_history_id = latest_history_id or get_profile_history_id()
    update_watch_state(last_history_id=new_history_id, last_checked_at=now)

    return {
        "status": "ok",
        "processed": processed,
        "alerts": alerts,
        "history_id": new_history_id,
    }
