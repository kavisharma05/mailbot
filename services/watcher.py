import asyncio
import logging
from datetime import datetime, timedelta, timezone
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
from services.alert_delivery import deliver_attachments, send_actionable_alert, send_owner_update
from services.attachments import annotate_attachment_intents
from services.contacts import upsert_contact_from_sender
from services.db import (
    clear_assignment_reminder,
    dequeue_pending_email,
    enqueue_pending_email,
    get_watch_state,
    insert_assignment,
    is_email_processed_cached,
    list_pending_email_ids,
    log_low_confidence,
    mark_email_processed,
    update_watch_state,
)
from services.formatting import (
    CATEGORY_LABELS,
    escape_html,
    format_deadline,
    is_priority_item,
    normalize_category,
    priority_prefix,
)
from services.schedule import (
    format_schedule_share,
    looks_like_schedule,
    parse_schedule_sessions,
    store_schedule_sessions,
)
from services.telegram_util import send_html
from services.timeutil import IST, now_ist, parse_iso_deadline

logger = logging.getLogger(__name__)

WATCHER_INTERVAL_SECONDS = 60
CATCHUP_DAYS = 3
CATCHUP_MAX_RESULTS = 120
watcher_lock = asyncio.Lock()


def _category_group_chat_id(category: str, settings: Settings) -> str | None:
    """Category group only — certification falls back to placements."""
    mapping = {
        "deadline": settings.telegram_chat_deadlines,
        "placement": settings.telegram_chat_placements,
        "certification": settings.telegram_chat_certification or settings.telegram_chat_placements,
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
    confidence: str | None = None,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    lines = [priority_prefix(category, task=task, summary=summary)]
    lines.append("📩 <b>New mail</b>")
    lines.append(f"<b>From:</b> {escape_html(sender or 'Unknown sender')}")
    lines.append(f"<b>Subject:</b> {escape_html(subject or '(no subject)')}")
    lines.append(f"<b>Category:</b> {escape_html(label)}")
    if confidence == "low":
        lines.append("<i>Low confidence — please review</i>")

    if course_or_topic:
        lines.append(f"<b>Course:</b> {escape_html(str(course_or_topic))}")
    if task:
        lines.append(f"<b>Task:</b> {escape_html(str(task))}")

    due = format_deadline(deadline_date, deadline_text)
    if due and due != "Not specified":
        lines.append(f"<b>Due:</b> {escape_html(due)}")

    if summary:
        lines.append("")
        lines.append(escape_html(str(summary)))

    return "\n".join(lines)


def format_alert_message(
    *,
    category: str,
    course_or_topic: str | None,
    task: str | None,
    deadline_date: str | None,
    deadline_text: str | None,
    summary: str | None,
    sender: str | None,
    subject: str | None = None,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    lines = [priority_prefix(category, task=task, summary=summary)]
    lines.append(f"<b>{escape_html(label)}</b>")
    if subject:
        lines.append(f"<b>Subject:</b> {escape_html(subject)}")

    if course_or_topic:
        lines.append(f"<b>Course:</b> {escape_html(str(course_or_topic))}")
    if task:
        lines.append(f"<b>Task:</b> {escape_html(str(task))}")

    due = format_deadline(deadline_date, deadline_text)
    lines.append(f"<b>Due:</b> {escape_html(due)}")

    if sender:
        lines.append(f"<b>From:</b> {escape_html(str(sender))}")

    if summary:
        lines.append("")
        lines.append(escape_html(str(summary)))

    return "\n".join(lines)


async def send_category_alert(
    bot,
    category: str,
    *,
    assignment_id: str,
    course_or_topic: str | None = None,
    task: str | None = None,
    deadline_date: str | None = None,
    deadline_text: str | None = None,
    summary: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> None:
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
        subject=subject,
    )
    pin = is_priority_item(category, task=task, summary=summary)
    await send_actionable_alert(
        bot,
        chat_id,
        text,
        assignment_id=assignment_id,
        attachments=attachments,
        caption_prefix=summary or task,
        pin=pin,
    )


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
        text = f"{text}\n\n<i>{escape_html(summary)}</i>"
    chat_id = _category_group_chat_id("admin", settings)
    if chat_id:
        await send_html(bot, chat_id, text)
        await deliver_attachments(bot, chat_id, attachments or [], caption_prefix="Class schedule")


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
    return confidence in ("high", "medium", "low")


def _should_owner_dm(
    settings: Settings,
    *,
    is_actionable: bool,
    category: str,
    task: str | None,
    summary: str | None,
) -> bool:
    if settings.owner_dm_all_mail:
        return True
    if is_actionable:
        return True
    return is_priority_item(category, task=task, summary=summary)


def _assignment_payload(
    result: dict[str, Any],
    details: dict[str, Any],
    category: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "source": "email",
        "category": category,
        "course_or_topic": result.get("course_or_topic"),
        "task": result.get("task"),
        "deadline_text": result.get("deadline_text"),
        "deadline_date": result.get("deadline_date"),
        "gmail_thread_id": details["thread_id"],
        "gmail_message_id": details["message_id"],
        "summary": result.get("summary"),
        "confidence": confidence,
        "status": "open",
    }


async def _process_message(
    message_id: str,
    bot,
    *,
    skip_past_deadlines: bool = False,
) -> dict[str, Any]:
    """Classify one email, persist results, optionally alert + forward attachments."""
    details = await asyncio.to_thread(get_message_details, message_id, load_attachments=True)
    await asyncio.to_thread(upsert_contact_from_sender, details.get("sender"))
    attachments = annotate_attachment_intents(
        details.get("attachments") or [],
        email_subject=details.get("subject") or "",
        email_body=details.get("body") or "",
    )
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
        await asyncio.to_thread(enqueue_pending_email, message_id, "classification_failed")
        return {"processed": 0, "alerts": 0, "transient": True}

    await asyncio.to_thread(dequeue_pending_email, message_id)

    is_actionable = result.get("is_actionable", False)
    confidence = result.get("confidence", "low")
    category = normalize_category(result.get("category"))
    deadline_date = result.get("deadline_date")
    assignment_id = None
    alerts = 0
    sender = result.get("sender") or details.get("sender")
    summary = result.get("summary")
    settings = get_settings()

    owner_text = format_incoming_mail_message(
        subject=details.get("subject"),
        sender=sender,
        category=category,
        course_or_topic=result.get("course_or_topic"),
        task=result.get("task"),
        deadline_date=deadline_date,
        deadline_text=result.get("deadline_text"),
        summary=summary,
        confidence=confidence,
    )
    pin_owner = is_priority_item(category, task=result.get("task"), summary=summary)

    # Timetable / weekly schedule path
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
            if _should_owner_dm(
                settings,
                is_actionable=is_actionable,
                category=category,
                task=result.get("task"),
                summary=summary,
            ):
                await send_owner_update(
                    bot,
                    settings.telegram_owner_chat_id,
                    owner_text,
                    attachments=attachments if is_actionable else None,
                    pin=pin_owner,
                )
            await share_schedule(
                bot,
                sessions=stored or sessions,
                summary=summary,
                attachments=attachments,
            )
            alerts = 1
            if is_actionable and confidence in ("high", "medium"):
                assignment = await asyncio.to_thread(
                    insert_assignment,
                    {
                        **_assignment_payload(result, details, "admin", confidence),
                        "course_or_topic": result.get("course_or_topic") or "Class schedule",
                        "task": result.get("task") or "New class timetable received",
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

    if _should_owner_dm(
        settings,
        is_actionable=is_actionable,
        category=category,
        task=result.get("task"),
        summary=summary,
    ) and not is_actionable:
        await send_owner_update(
            bot,
            settings.telegram_owner_chat_id,
            owner_text,
            pin=pin_owner,
        )

    if is_actionable:
        assignment = await asyncio.to_thread(
            insert_assignment,
            _assignment_payload(result, details, category, confidence),
        )
        if assignment:
            assignment_id = assignment["id"]

        if assignment_id and _should_owner_dm(
            settings,
            is_actionable=True,
            category=category,
            task=result.get("task"),
            summary=summary,
        ):
            await send_owner_update(
                bot,
                settings.telegram_owner_chat_id,
                owner_text,
                assignment_id=str(assignment_id),
                attachments=attachments,
                pin=pin_owner,
            )

        if assignment_id and _should_alert(
            confidence, deadline_date, skip_past_deadlines=skip_past_deadlines
        ):
            await send_category_alert(
                bot,
                category,
                assignment_id=str(assignment_id),
                course_or_topic=result.get("course_or_topic"),
                task=result.get("task"),
                deadline_date=deadline_date,
                deadline_text=result.get("deadline_text"),
                summary=summary,
                sender=sender,
                subject=details.get("subject"),
                attachments=attachments,
            )
            alerts = 1
    elif confidence == "low":
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


def _recent_unprocessed_message_ids(max_results: int = CATCHUP_MAX_RESULTS) -> list[str]:
    try:
        return search_message_ids(f"newer_than:{CATCHUP_DAYS}d", max_results=max_results)
    except Exception as exc:
        logger.warning("Recent catch-up Gmail search failed: %s", exc)
        return []


async def _collect_message_ids(state: dict[str, Any] | None) -> tuple[list[str], str | None]:
    if not state or not state.get("last_history_id"):
        history_id = await asyncio.to_thread(get_profile_history_id)
        catchup_ids = await asyncio.to_thread(_recent_unprocessed_message_ids)
        pending = await asyncio.to_thread(list_pending_email_ids)
        merged = list(dict.fromkeys(pending + catchup_ids))
        return merged, history_id

    start_id = state["last_history_id"]
    message_ids, latest_history_id = await asyncio.to_thread(list_history_message_ids, start_id)

    seen: set[str] = set(message_ids)
    for mid in await asyncio.to_thread(_recent_unprocessed_message_ids):
        if mid not in seen:
            message_ids.append(mid)
            seen.add(mid)
    for mid in await asyncio.to_thread(list_pending_email_ids):
        if mid not in seen:
            message_ids.append(mid)
            seen.add(mid)

    return message_ids, latest_history_id


async def run_gmail_watcher(bot) -> dict:
    """Poll Gmail for new emails, classify, route alerts by category."""
    state = await asyncio.to_thread(get_watch_state)
    now = datetime.now(timezone.utc)

    message_ids, latest_or_init = await _collect_message_ids(state)

    if not state or not state.get("last_history_id"):
        history_id = latest_or_init or await asyncio.to_thread(get_profile_history_id)
        await asyncio.to_thread(update_watch_state, last_history_id=history_id, last_checked_at=now)
        logger.info("Initialized watch state at historyId=%s — running catch-up", history_id)
        processed = 0
        alerts = 0
        for message_id in message_ids:
            if await asyncio.to_thread(is_email_processed_cached, message_id):
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
            "catchup_candidates": len(message_ids),
        }

    start_id = state["last_history_id"]
    processed = 0
    alerts = 0
    failed = 0

    for message_id in message_ids:
        if await asyncio.to_thread(is_email_processed_cached, message_id):
            continue

        try:
            outcome = await _process_message(message_id, bot)
        except Exception as exc:
            logger.exception("Processing failed for %s: %s", message_id, exc)
            failed += 1
            continue
        processed += outcome["processed"]
        alerts += outcome["alerts"]

    if failed == 0:
        new_history_id = latest_or_init or await asyncio.to_thread(get_profile_history_id)
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
        if await asyncio.to_thread(is_email_processed_cached, message_id):
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


async def run_due_reminders(bot) -> dict:
    """Re-send alerts for assignments whose remind_at has passed."""
    from services.db import get_assignments_due_reminder

    due = await asyncio.to_thread(get_assignments_due_reminder)
    sent = 0
    settings = get_settings()

    for item in due:
        assignment_id = item.get("id")
        if not assignment_id:
            continue
        category = normalize_category(item.get("category"))
        text = format_alert_message(
            category=category,
            course_or_topic=item.get("course_or_topic"),
            task=item.get("task"),
            deadline_date=item.get("deadline_date"),
            deadline_text=item.get("deadline_text"),
            summary=item.get("summary"),
            sender=None,
            subject="Reminder",
        )
        text = f"⏰ <b>Reminder</b>\n\n{text}"
        chat_id = _category_group_chat_id(category, settings)
        if chat_id:
            await send_actionable_alert(
                bot,
                chat_id,
                text,
                assignment_id=str(assignment_id),
                pin=is_priority_item(category, task=item.get("task"), summary=item.get("summary")),
            )
        if settings.telegram_owner_chat_id:
            await send_actionable_alert(
                bot,
                settings.telegram_owner_chat_id,
                text,
                assignment_id=str(assignment_id),
            )
        await asyncio.to_thread(clear_assignment_reminder, str(assignment_id))
        sent += 1

    return {"status": "ok", "reminders_sent": sent}


# Re-export for digest imports
_escape_html = escape_html
_format_deadline_display = format_deadline
CATEGORY_LABELS = CATEGORY_LABELS
