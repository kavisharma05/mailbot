import logging
from datetime import datetime, timezone
from typing import Any

from ai.deadline import classify_email
from config import Settings, get_settings
from gmail_api.client import (
    get_message_details,
    get_profile_history_id,
    list_history_message_ids,
    list_inbox_message_ids_since,
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

VALID_CATEGORIES = ("deadline", "placement", "admin", "other")

CATEGORY_LABELS = {
    "deadline": "Deadline alert",
    "placement": "Placement alert",
    "admin": "Admin alert",
    "other": "Mail alert",
}


def _resolve_chat_id(category: str, settings: Settings) -> str:
    mapping = {
        "deadline": settings.telegram_chat_deadlines,
        "placement": settings.telegram_chat_placements,
        "admin": settings.telegram_chat_admin,
        "other": settings.telegram_chat_other,
    }
    chat_id = mapping.get(category, "") or settings.telegram_owner_chat_id
    return chat_id


async def send_telegram_alert(summary: str, bot, category: str) -> None:
    settings = get_settings()
    chat_id = _resolve_chat_id(category, settings)
    label = CATEGORY_LABELS.get(category, "Mail alert")
    await bot.send_message(chat_id=chat_id, text=f"{label}: {summary}")


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


def _normalize_category(value: str | None) -> str:
    if value in VALID_CATEGORIES:
        return value
    return "other"


def _deadline_is_past(deadline_date: str | None) -> bool:
    if not deadline_date:
        return False
    try:
        dt = datetime.fromisoformat(deadline_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc)
    except ValueError:
        return False


def _should_alert(
    is_actionable: bool,
    confidence: str,
    deadline_date: str | None,
    *,
    skip_past_deadlines: bool,
) -> bool:
    if not is_actionable or confidence not in ("high", "medium"):
        return False
    if skip_past_deadlines and _deadline_is_past(deadline_date):
        return False
    return True


async def _process_message(
    message_id: str,
    bot,
    *,
    skip_past_deadlines: bool = False,
) -> dict[str, Any]:
    """Classify one email, persist results, optionally alert."""
    details = get_message_details(message_id)
    result = classify_email(details["subject"], details["body"], details["sender"])

    if result is None:
        log_low_confidence(
            "deadline_classifier",
            {"message_id": message_id, "subject": details["subject"]},
        )
        mark_email_processed(message_id, None, None)
        return {"processed": 1, "alerts": 0}

    is_actionable = result.get("is_actionable", False)
    confidence = result.get("confidence", "low")
    category = _normalize_category(result.get("category"))
    deadline_date = _parse_deadline_date(result.get("deadline_date"))
    assignment_id = None
    alerts = 0

    if is_actionable and confidence in ("high", "medium"):
        assignment = insert_assignment(
            {
                "source": "email",
                "category": category,
                "course_or_topic": result.get("course_or_topic"),
                "task": result.get("task"),
                "deadline_text": result.get("deadline_text"),
                "deadline_date": deadline_date,
                "gmail_thread_id": details["thread_id"],
                "gmail_message_id": details["message_id"],
                "summary": result.get("summary"),
                "confidence": confidence,
                "status": "open",
            }
        )
        assignment_id = assignment["id"]

        summary = result.get("summary")
        if summary and _should_alert(
            is_actionable,
            confidence,
            deadline_date,
            skip_past_deadlines=skip_past_deadlines,
        ):
            await send_telegram_alert(summary, bot, category)
            alerts = 1
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
    return {"processed": 1, "alerts": alerts}


async def run_gmail_watcher(bot) -> dict:
    """Poll Gmail for new emails, classify, route alerts by category."""
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

        outcome = await _process_message(message_id, bot)
        processed += outcome["processed"]
        alerts += outcome["alerts"]

    new_history_id = latest_history_id or get_profile_history_id()
    update_watch_state(last_history_id=new_history_id, last_checked_at=now)

    return {
        "status": "ok",
        "processed": processed,
        "alerts": alerts,
        "history_id": new_history_id,
    }


async def run_gmail_backfill(bot, days: int = 14) -> dict:
    """One-time scan of recent inbox mail. Does not update watch_state."""
    message_ids = list_inbox_message_ids_since(days=days)

    processed = 0
    alerts = 0
    skipped = 0

    for message_id in message_ids:
        if is_email_processed(message_id):
            skipped += 1
            continue

        outcome = await _process_message(message_id, bot, skip_past_deadlines=True)
        processed += outcome["processed"]
        alerts += outcome["alerts"]

    return {
        "status": "ok",
        "days": days,
        "candidates": len(message_ids),
        "skipped_already_processed": skipped,
        "processed": processed,
        "alerts": alerts,
    }
