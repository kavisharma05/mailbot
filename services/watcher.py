import asyncio
import io
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
    search_message_ids,
)
from services.db import (
    get_watch_state,
    insert_assignment,
    is_email_processed,
    log_low_confidence,
    mark_email_processed,
    update_watch_state,
)
from services.schedule import (
    format_schedule_share,
    looks_like_schedule,
    parse_schedule_sessions,
    store_schedule_sessions,
)
from services.telegram_util import send_html
from services.timeutil import (
    IST,
    format_deadline_display,
    now_ist,
    parse_iso_deadline,
)

logger = logging.getLogger(__name__)

WATCHER_INTERVAL_SECONDS = 60
watcher_lock = asyncio.Lock()

VALID_CATEGORIES = ("deadline", "placement", "admin", "other")

CATEGORY_LABELS = {
    "deadline": "Deadline",
    "placement": "Placement",
    "admin": "Admin",
    "other": "Other",
}


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_deadline_display(deadline_date: str | None, deadline_text: str | None) -> str:
    return format_deadline_display(deadline_date, deadline_text, tz=IST)


def _category_group_chat_id(category: str, settings: Settings) -> str | None:
    """Category group only — no owner fallback."""
    mapping = {
        "deadline": settings.telegram_chat_deadlines,
        "placement": settings.telegram_chat_placements,
        "admin": settings.telegram_chat_admin,
        "other": settings.telegram_chat_other,
    }
    chat_id = mapping.get(category, "") or ""
    return chat_id or None


def format_incoming_mail_message(
    *,
    subject: str | None,
    sender: str | None,
    category: str,
    course_or_topic: str | None = None,
    task: str | None = None,
    deadline_date: str | None = None,
    deadline_text: str | None = None,
    summary: str | None = None,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    lines = ["📩 <b>New mail</b>"]
    lines.append(f"<b>From:</b> {_escape_html(sender or 'Unknown sender')}")
    lines.append(f"<b>Subject:</b> {_escape_html(subject or '(no subject)')}")
    lines.append(f"<b>Category:</b> {_escape_html(label)}")

    if course_or_topic:
        lines.append(f"<b>Course:</b> {_escape_html(str(course_or_topic))}")
    if task:
        lines.append(f"<b>Task:</b> {_escape_html(str(task))}")

    due = _format_deadline_display(deadline_date, deadline_text)
    if due and due != "Not specified":
        lines.append(f"<b>Due:</b> {_escape_html(due)}")

    if summary:
        lines.append("")
        lines.append(_escape_html(str(summary)))

    return "\n".join(lines)


async def send_owner_mail_update(
    bot,
    text: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    settings = get_settings()
    chat_id = settings.telegram_owner_chat_id
    if not chat_id:
        return
    await send_html(bot, chat_id, text)
    if attachments:
        await _forward_attachments(bot, chat_id, attachments, caption_prefix=None)


def format_alert_message(
    *,
    category: str,
    course_or_topic: str | None,
    task: str | None,
    deadline_date: str | None,
    deadline_text: str | None,
    summary: str | None,
    sender: str | None,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    lines = [f"<b>{_escape_html(label)}</b>"]

    if course_or_topic:
        lines.append(f"<b>Course:</b> {_escape_html(str(course_or_topic))}")
    if task:
        lines.append(f"<b>Task:</b> {_escape_html(str(task))}")

    due = _format_deadline_display(deadline_date, deadline_text)
    lines.append(f"<b>Due:</b> {_escape_html(due)}")

    if sender:
        lines.append(f"<b>From:</b> {_escape_html(str(sender))}")

    if summary:
        lines.append("")
        lines.append(_escape_html(str(summary)))

    return "\n".join(lines)


async def send_category_alert(
    bot,
    category: str,
    *,
    course_or_topic: str | None = None,
    task: str | None = None,
    deadline_date: str | None = None,
    deadline_text: str | None = None,
    summary: str | None = None,
    sender: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    """Actionable alert to category group only (owner gets separate rich update)."""
    settings = get_settings()
    chat_id = _category_group_chat_id(category, settings)
    if not chat_id:
        return
    text = format_alert_message(
        category=category,
        course_or_topic=course_or_topic,
        task=task,
        deadline_date=deadline_date,
        deadline_text=deadline_text,
        summary=summary,
        sender=sender,
    )
    await send_html(bot, chat_id, text)
    await _forward_attachments(bot, chat_id, attachments or [], caption_prefix=summary or task)


async def _forward_attachments(
    bot,
    chat_id: str,
    attachments: list[dict[str, Any]],
    *,
    caption_prefix: str | None = None,
) -> int:
    sent = 0
    for att in attachments:
        if not att.get("forward"):
            continue
        data = att.get("data")
        filename = att.get("filename") or "attachment"
        if not data:
            continue
        caption = filename
        if caption_prefix:
            caption = f"{str(caption_prefix)[:80]}\n{filename}"
        try:
            from telegram import InputFile

            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption[:1000],
            )
            sent += 1
        except Exception as exc:
            logger.warning("Failed sending attachment %s to %s: %s", filename, chat_id, exc)
    return sent


async def share_schedule(
    bot,
    *,
    sessions: list[dict[str, Any]],
    summary: str | None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
    settings = get_settings()
    text = format_schedule_share(sessions)
    if summary:
        text = f"{text}\n\n<i>{_escape_html(summary)}</i>"
    chat_id = _category_group_chat_id("admin", settings)
    if chat_id:
        await send_html(bot, chat_id, text)
        await _forward_attachments(bot, chat_id, attachments or [], caption_prefix="Class schedule")


def _normalize_category(value: str | None) -> str:
    if value in VALID_CATEGORIES:
        return value
    return "other"


def _deadline_is_past(deadline_date: str | None) -> bool:
    dt = parse_iso_deadline(deadline_date)
    if dt is None:
        return False
    return dt < now_ist()


def _should_alert(
    confidence: str,
    deadline_date: str | None,
    *,
    skip_past_deadlines: bool,
) -> bool:
    if skip_past_deadlines and _deadline_is_past(deadline_date):
        return False
    return True


async def _process_message(
    message_id: str,
    bot,
    *,
    skip_past_deadlines: bool = False,
) -> dict[str, Any]:
    """Classify one email, persist results, optionally alert + forward attachments."""
    details = await asyncio.to_thread(get_message_details, message_id, load_attachments=True)
    attachments = details.get("attachments") or []
    attachment_text = details.get("attachment_text") or ""

    result = await asyncio.to_thread(
        classify_email,
        details["subject"],
        details["body"],
        details["sender"],
        received_at=details.get("received_at_iso") or details.get("received_at"),
        attachment_text=attachment_text,
    )

    if result is None:
        await asyncio.to_thread(
            log_low_confidence,
            "deadline_classifier",
            {"message_id": message_id, "subject": details["subject"], "retry": True},
        )
        return {"processed": 0, "alerts": 0, "transient": True}

    is_actionable = result.get("is_actionable", False)
    confidence = result.get("confidence", "low")
    category = _normalize_category(result.get("category"))
    deadline_date = result.get("deadline_date")
    assignment_id = None
    alerts = 0
    sender = result.get("sender") or details.get("sender")
    summary = result.get("summary")

    owner_text = format_incoming_mail_message(
        subject=details.get("subject"),
        sender=sender,
        category=category,
        course_or_topic=result.get("course_or_topic"),
        task=result.get("task"),
        deadline_date=deadline_date,
        deadline_text=result.get("deadline_text"),
        summary=summary,
    )

    # Timetable / weekly schedule path (keyword/PDF match — don't require AI confidence)
    if looks_like_schedule(
        details["subject"],
        details["body"],
        attachment_text,
        result,
    ):
        sessions = await asyncio.to_thread(
            parse_schedule_sessions,
            subject=details["subject"],
            body=details["body"],
            attachment_text=attachment_text,
            received_at=details.get("received_at_iso"),
        )
        if sessions:
            stored = await asyncio.to_thread(
                store_schedule_sessions,
                sessions,
                gmail_message_id=message_id,
            )
            await send_owner_mail_update(
                bot,
                owner_text,
                attachments=attachments if is_actionable else None,
            )
            await share_schedule(
                bot,
                sessions=stored or sessions,
                summary=summary,
                attachments=attachments,
            )
            alerts = 1
            # Also keep a lightweight assignment so /due shows the schedule notice
            if is_actionable and confidence in ("high", "medium"):
                assignment = await asyncio.to_thread(
                    insert_assignment,
                    {
                        "source": "email",
                        "category": "admin",
                        "course_or_topic": result.get("course_or_topic") or "Class schedule",
                        "task": result.get("task") or "New class timetable received",
                        "deadline_text": result.get("deadline_text"),
                        "deadline_date": deadline_date,
                        "gmail_thread_id": details["thread_id"],
                        "gmail_message_id": details["message_id"],
                        "summary": result.get("summary"),
                        "confidence": confidence,
                        "status": "open",
                    },
                )
                if assignment:
                    assignment_id = assignment["id"]
            await asyncio.to_thread(
                mark_email_processed,
                message_id,
                True,
                confidence or "medium",
                assignment_id,
            )
            return {"processed": 1, "alerts": alerts}

    await send_owner_mail_update(
        bot,
        owner_text,
        attachments=attachments if is_actionable and confidence in ("high", "medium") else None,
    )

    if is_actionable and confidence in ("high", "medium"):
        assignment = await asyncio.to_thread(
            insert_assignment,
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
            },
        )
        if assignment:
            assignment_id = assignment["id"]

        if _should_alert(
            confidence,
            deadline_date,
            skip_past_deadlines=skip_past_deadlines,
        ):
            await send_category_alert(
                bot,
                category,
                course_or_topic=result.get("course_or_topic"),
                task=result.get("task"),
                deadline_date=deadline_date,
                deadline_text=result.get("deadline_text"),
                summary=summary,
                sender=sender,
                attachments=attachments,
            )
            alerts = 1
    elif is_actionable or confidence == "low":
        await asyncio.to_thread(
            log_low_confidence,
            "deadline_classifier",
            {
                "message_id": message_id,
                "subject": details["subject"],
                "result": result,
            },
        )

    await asyncio.to_thread(
        mark_email_processed,
        message_id,
        is_actionable,
        confidence,
        assignment_id,
    )
    return {"processed": 1, "alerts": alerts}


def _recent_unprocessed_message_ids(max_results: int = 40) -> list[str]:
    """Safety net: history API can skip mail if historyId advanced after a failed classify."""
    try:
        return search_message_ids("newer_than:2d", max_results=max_results)
    except Exception as exc:
        logger.warning("Recent catch-up Gmail search failed: %s", exc)
        return []


async def run_gmail_watcher(bot) -> dict:
    """Poll Gmail for new emails, classify, route alerts by category."""
    state = await asyncio.to_thread(get_watch_state)
    now = datetime.now(timezone.utc)

    if not state or not state.get("last_history_id"):
        history_id = await asyncio.to_thread(get_profile_history_id)
        await asyncio.to_thread(update_watch_state, last_history_id=history_id, last_checked_at=now)
        logger.info("Initialized watch state at historyId=%s — running 2-day catch-up", history_id)
        # Don't silently skip recent mail on (re)init
        catchup_ids = await asyncio.to_thread(_recent_unprocessed_message_ids, max_results=60)
        processed = 0
        alerts = 0
        for message_id in catchup_ids:
            if await asyncio.to_thread(is_email_processed, message_id):
                continue
            try:
                outcome = await _process_message(message_id, bot)
            except Exception as exc:
                logger.exception("Catch-up processing failed for %s: %s", message_id, exc)
                continue
            processed += outcome["processed"]
            alerts += outcome["alerts"]
        return {
            "status": "initialized",
            "history_id": history_id,
            "processed": processed,
            "alerts": alerts,
            "catchup_candidates": len(catchup_ids),
        }

    start_id = state["last_history_id"]
    message_ids, latest_history_id = await asyncio.to_thread(list_history_message_ids, start_id)

    # Merge recent inbox/spam scan so failed/skipped history items still get retried
    seen: set[str] = set(message_ids)
    for mid in await asyncio.to_thread(_recent_unprocessed_message_ids):
        if mid not in seen:
            message_ids.append(mid)
            seen.add(mid)

    processed = 0
    alerts = 0
    failed = 0

    for message_id in message_ids:
        if await asyncio.to_thread(is_email_processed, message_id):
            continue

        try:
            outcome = await _process_message(message_id, bot)
        except Exception as exc:
            logger.exception("Processing failed for %s: %s", message_id, exc)
            failed += 1
            continue
        processed += outcome["processed"]
        alerts += outcome["alerts"]

    # Advance history unless a message raised — transient classify failures retry via catch-up
    if failed == 0:
        new_history_id = latest_history_id or await asyncio.to_thread(get_profile_history_id)
        await asyncio.to_thread(
            update_watch_state,
            last_history_id=new_history_id,
            last_checked_at=now,
        )
    else:
        new_history_id = start_id
        await asyncio.to_thread(update_watch_state, last_checked_at=now)
        logger.warning(
            "Deferred history advance: %d message(s) raised errors and will retry",
            failed,
        )

    return {
        "status": "ok",
        "processed": processed,
        "alerts": alerts,
        "failed_retries": failed,
        "history_id": new_history_id,
    }


async def run_gmail_backfill(bot, days: int = 14) -> dict:
    """One-time scan of recent inbox mail. Does not update watch_state."""
    message_ids = await asyncio.to_thread(list_inbox_message_ids_since, days=days)

    processed = 0
    alerts = 0
    skipped = 0

    for message_id in message_ids:
        if await asyncio.to_thread(is_email_processed, message_id):
            skipped += 1
            continue

        try:
            outcome = await _process_message(message_id, bot, skip_past_deadlines=True)
        except Exception as exc:
            logger.exception("Backfill processing failed for %s: %s", message_id, exc)
            continue
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
