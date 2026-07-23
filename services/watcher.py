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
)
from services.alert_delivery import deliver_attachments, send_actionable_alert, send_owner_update
from services.attachments import annotate_attachment_intents
from services.contacts import parse_sender, upsert_contact_from_sender
from services.db import (
    clear_assignment_reminder,
    dequeue_pending_email,
    enqueue_pending_email,
    get_pending_email,
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
    category_icon,
    escape_html,
    format_deadline,
    is_priority_item,
    normalize_category,
    priority_prefix,
)
from services.schedule import (
    format_schedule_share,
    is_trusted_schedule_sender,
    looks_like_schedule,
    parse_schedule_sessions,
    store_schedule_sessions,
)
from services.reminders import run_scheduled_reminders, schedule_deadline_reminders
from services.telegram_util import send_html
from services.timeutil import IST, now_ist, parse_iso_deadline

logger = logging.getLogger(__name__)

WATCHER_INTERVAL_SECONDS = 60
CATCHUP_DAYS = 14
CATCHUP_MAX_RESULTS = 300
watcher_lock = asyncio.Lock()

_REUSE_CLASSIFICATION_ERRORS = frozenset({"alert_delivery_failed", "mark_failed"})


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


def format_mail_alert(
    *,
    header: str,
    subject: str | None = None,
    sender: str | None = None,
    category: str,
    course_or_topic: str | None = None,
    task: str | None = None,
    deadline_date: str | None = None,
    deadline_text: str | None = None,
    summary: str | None = None,
    confidence: str | None = None,
) -> str:
    """Unified owner/group alert body."""
    label = CATEGORY_LABELS.get(category, "Mail")
    icon = category_icon(category)
    lines = [
        priority_prefix(
            category,
            task=task,
            summary=summary,
            deadline_date=deadline_date,
        ),
        header,
        f"<b>From:</b> {escape_html(sender or 'Unknown sender')}",
        f"<b>Subject:</b> {escape_html(subject or '(no subject)')}",
    ]
    if task:
        lines.append(f"<b>Task:</b> {escape_html(str(task))}")
    due = format_deadline(deadline_date, deadline_text)
    if due and due != "Not specified":
        lines.append(f"<b>Due:</b> {escape_html(due)}")
    lines.append(f"<b>Category:</b> {icon} {escape_html(label)}")
    if confidence == "low":
        lines.append("<i>Low confidence — please review</i>")
    if course_or_topic:
        lines.append(f"<b>Course:</b> {escape_html(str(course_or_topic))}")
    if summary:
        lines.append("")
        lines.append(escape_html(str(summary)))
    return "\n".join(lines)


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
    return format_mail_alert(
        header="📩 <b>New mail</b>",
        subject=subject,
        sender=sender,
        category=category,
        course_or_topic=course_or_topic,
        task=task,
        deadline_date=deadline_date,
        deadline_text=deadline_text,
        summary=summary,
        confidence=confidence,
    )


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
    header: str | None = None,
) -> str:
    return format_mail_alert(
        header=header or "📩 <b>New mail</b>",
        subject=subject,
        sender=sender,
        category=category,
        course_or_topic=course_or_topic,
        task=task,
        deadline_date=deadline_date,
        deadline_text=deadline_text,
        summary=summary,
    )


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
) -> bool:
    settings = get_settings()
    chat_id = _category_group_chat_id(category, settings)
    if not chat_id:
        return True
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
    pin = is_priority_item(
        category,
        task=task,
        summary=summary,
        deadline_date=deadline_date,
    )
    msg_id = await send_actionable_alert(
        bot,
        chat_id,
        text,
        assignment_id=assignment_id,
        attachments=attachments,
        caption_prefix=summary or task,
        pin=pin,
    )
    return msg_id is not None


async def share_schedule(
    bot,
    *,
    sessions: list[dict[str, Any]],
    summary: str | None,
    attachments: list[dict[str, Any]] | None = None,
) -> bool:
    settings = get_settings()
    text = format_schedule_share(sessions)
    if summary:
        text = f"{text}\n\n<i>{escape_html(summary)}</i>"
    chat_id = _category_group_chat_id("admin", settings)
    if not chat_id:
        return True
    html_ok = await send_html(bot, chat_id, text)
    attach_ok = await deliver_attachments(bot, chat_id, attachments or [], caption_prefix="Class schedule")
    return html_ok and attach_ok


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
    return confidence in ("high", "medium")


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
    return is_priority_item(
        category,
        task=task,
        summary=summary,
        deadline_date=None,
    )


def _assignment_payload(
    result: dict[str, Any],
    details: dict[str, Any],
    category: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "source": "email",
        "category": category,
        "item_type": result.get("item_type"),
        "priority": result.get("priority"),
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


def _is_own_sent_mail(details: dict[str, Any], settings: Settings) -> bool:
    """True for outbound mail you sent that did not land in inbox."""
    label_ids = details.get("label_ids") or []
    if "INBOX" in label_ids:
        return False
    if "SENT" in label_ids:
        return True
    _, sender_email = parse_sender(details.get("sender"))
    owner = (settings.gmail_user or "").strip().lower()
    return bool(owner and sender_email and sender_email.lower() == owner)


async def _process_message(
    message_id: str,
    bot,
    *,
    skip_past_deadlines: bool = False,
) -> dict[str, Any]:
    """Classify one email, persist results, optionally alert + forward attachments."""
    pending_row = await asyncio.to_thread(get_pending_email, message_id)
    if pending_row and pending_row.get("dead_letter"):
        await asyncio.to_thread(
            mark_email_processed,
            message_id,
            False,
            None,
            None,
            delivery_failed=True,
        )
        await asyncio.to_thread(dequeue_pending_email, message_id)
        return {"processed": 1, "alerts": 0}

    details = await asyncio.to_thread(get_message_details, message_id, load_attachments=True)

    settings = get_settings()
    if _is_own_sent_mail(details, settings):
        await asyncio.to_thread(dequeue_pending_email, message_id)
        await asyncio.to_thread(
            mark_email_processed,
            message_id,
            False,
            "high",
            None,
        )
        return {"processed": 0, "alerts": 0}

    await asyncio.to_thread(upsert_contact_from_sender, details.get("sender"))
    attachments = annotate_attachment_intents(
        details.get("attachments") or [],
        email_subject=details.get("subject") or "",
        email_body=details.get("body") or "",
    )
    attachment_text = details.get("attachment_text") or ""

    already_alerted = bool((pending_row or {}).get("already_alerted"))
    cached_result = None
    if pending_row and pending_row.get("last_error") in _REUSE_CLASSIFICATION_ERRORS:
        cached_result = pending_row.get("classified_result")

    if cached_result:
        result = cached_result
    else:
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
    delivery_ok = True
    skip_alerts = already_alerted

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
    pin_owner = is_priority_item(
        category,
        task=result.get("task"),
        summary=summary,
        deadline_date=deadline_date,
    )

    async def _fail_delivery(*, error: str = "alert_delivery_failed") -> dict[str, Any]:
        await asyncio.to_thread(
            enqueue_pending_email,
            message_id,
            error,
            classified_result=result,
            already_alerted=skip_alerts or error == "mark_failed",
        )
        return {"processed": 0, "alerts": 0, "transient": True}

    async def _finalize_processed() -> dict[str, Any] | None:
        marked = await asyncio.to_thread(
            mark_email_processed,
            message_id,
            is_actionable,
            confidence,
            assignment_id,
        )
        if marked:
            return None
        return await _fail_delivery(error="mark_failed")

    # Timetable / weekly schedule path
    if is_trusted_schedule_sender(details.get("sender", "")) and looks_like_schedule(
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
            if not skip_alerts:
                if _should_owner_dm(
                    settings,
                    is_actionable=is_actionable,
                    category=category,
                    task=result.get("task"),
                    summary=summary,
                ):
                    owner_sent = await send_owner_update(
                        bot,
                        settings.telegram_owner_chat_id,
                        owner_text,
                        attachments=attachments if is_actionable else None,
                        pin=pin_owner,
                    )
                    if not owner_sent:
                        delivery_ok = False
                schedule_sent = await share_schedule(
                    bot,
                    sessions=stored or sessions,
                    summary=summary,
                    attachments=attachments,
                )
                if not schedule_sent:
                    delivery_ok = False
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
                elif not skip_alerts:
                    return await _fail_delivery(error="assignment_insert_failed")
            if not delivery_ok:
                return await _fail_delivery()
            finalize = await _finalize_processed()
            if finalize:
                return finalize
            return {"processed": 1, "alerts": alerts}

    if not skip_alerts and _should_owner_dm(
        settings,
        is_actionable=is_actionable,
        category=category,
        task=result.get("task"),
        summary=summary,
    ) and not is_actionable:
        owner_sent = await send_owner_update(
            bot,
            settings.telegram_owner_chat_id,
            owner_text,
            pin=pin_owner,
        )
        if not owner_sent:
            delivery_ok = False

    if is_actionable:
        assignment = await asyncio.to_thread(
            insert_assignment,
            _assignment_payload(result, details, category, confidence),
        )
        if assignment:
            assignment_id = assignment["id"]
            await asyncio.to_thread(schedule_deadline_reminders, assignment)
        elif not skip_alerts:
            return await _fail_delivery(error="assignment_insert_failed")

        if not skip_alerts and assignment_id and _should_owner_dm(
            settings,
            is_actionable=True,
            category=category,
            task=result.get("task"),
            summary=summary,
        ):
            owner_sent = await send_owner_update(
                bot,
                settings.telegram_owner_chat_id,
                owner_text,
                assignment_id=str(assignment_id),
                attachments=attachments,
                pin=pin_owner,
            )
            if not owner_sent:
                delivery_ok = False

        if not skip_alerts and assignment_id and _should_alert(
            confidence, deadline_date, skip_past_deadlines=skip_past_deadlines
        ):
            category_sent = await send_category_alert(
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
            if not category_sent:
                delivery_ok = False
            else:
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

    if not delivery_ok:
        return await _fail_delivery()

    finalize = await _finalize_processed()
    if finalize:
        return finalize
    return {"processed": 1, "alerts": alerts}


def _recent_unprocessed_message_ids(max_results: int = CATCHUP_MAX_RESULTS) -> list[str]:
    try:
        return list_inbox_message_ids_since(days=CATCHUP_DAYS, max_results=max_results)
    except Exception as exc:
        logger.warning("Recent catch-up Gmail search failed: %s", exc)
        return []


async def _collect_message_ids(
    state: dict[str, Any] | None,
) -> tuple[list[str], str | None, bool]:
    if not state or not state.get("last_history_id"):
        history_id = await asyncio.to_thread(get_profile_history_id)
        catchup_ids = await asyncio.to_thread(_recent_unprocessed_message_ids)
        pending = await asyncio.to_thread(list_pending_email_ids)
        merged = list(dict.fromkeys(pending + catchup_ids))
        return merged, history_id, False

    start_id = state["last_history_id"]
    message_ids, latest_history_id, history_failed = await asyncio.to_thread(
        list_history_message_ids, start_id
    )

    seen: set[str] = set(message_ids)
    for mid in await asyncio.to_thread(_recent_unprocessed_message_ids):
        if mid not in seen:
            message_ids.append(mid)
            seen.add(mid)
    for mid in await asyncio.to_thread(list_pending_email_ids):
        if mid not in seen:
            message_ids.append(mid)
            seen.add(mid)

    return message_ids, latest_history_id, history_failed


async def run_gmail_watcher(bot) -> dict:
    """Poll Gmail for new emails, classify, route alerts by category."""
    from google.auth.exceptions import RefreshError

    try:
        return await _run_gmail_watcher_inner(bot)
    except RefreshError as exc:
        logger.exception("Gmail auth failed during watcher: %s", exc)
        await notify_gmail_auth_failure(bot)
        raise


async def _processed_lookup(message_id: str) -> bool | None:
    return await asyncio.to_thread(is_email_processed_cached, message_id)


async def _run_gmail_watcher_inner(bot) -> dict:
    state = await asyncio.to_thread(get_watch_state)
    now = datetime.now(timezone.utc)

    message_ids, latest_or_init, history_failed = await _collect_message_ids(state)

    if not state or not state.get("last_history_id"):
        history_id = latest_or_init or await asyncio.to_thread(get_profile_history_id)
        logger.info("Running init catch-up before setting historyId=%s", history_id)
        processed = 0
        alerts = 0
        failed = 0
        for message_id in message_ids:
            processed_state = await _processed_lookup(message_id)
            if processed_state is None:
                logger.warning(
                    "Deferring %s — processed state unknown (DB unavailable)",
                    message_id,
                )
                failed += 1
                continue
            if processed_state:
                continue
            try:
                outcome = await _process_message(message_id, bot)
            except Exception as exc:
                logger.exception("Catch-up processing failed for %s: %s", message_id, exc)
                failed += 1
                continue
            if outcome.get("transient"):
                failed += 1
                continue
            processed += outcome["processed"]
            alerts += outcome["alerts"]

        if failed == 0:
            await asyncio.to_thread(
                update_watch_state,
                last_history_id=history_id,
                last_checked_at=now,
            )
            logger.info("Initialized watch state at historyId=%s", history_id)
        else:
            await asyncio.to_thread(update_watch_state, last_checked_at=now)
            logger.warning(
                "Deferred init history advance: %d message(s) failed and will retry",
                failed,
            )
        return {
            "status": "initialized",
            "history_id": history_id if failed == 0 else None,
            "processed": processed,
            "alerts": alerts,
            "failed_retries": failed,
            "catchup_candidates": len(message_ids),
        }

    start_id = state["last_history_id"]
    processed = 0
    alerts = 0
    failed = 0

    for message_id in message_ids:
        processed_state = await _processed_lookup(message_id)
        if processed_state is None:
            logger.warning(
                "Deferring %s — processed state unknown (DB unavailable)",
                message_id,
            )
            failed += 1
            continue
        if processed_state:
            continue

        try:
            outcome = await _process_message(message_id, bot)
        except Exception as exc:
            logger.exception("Processing failed for %s: %s", message_id, exc)
            failed += 1
            continue
        if outcome.get("transient"):
            failed += 1
            logger.warning(
                "Deferred history advance: transient failure for %s (classification retry pending)",
                message_id,
            )
            continue
        processed += outcome["processed"]
        alerts += outcome["alerts"]

    if failed == 0 and not history_failed:
        new_history_id = latest_or_init or await asyncio.to_thread(get_profile_history_id)
        await asyncio.to_thread(
            update_watch_state,
            last_history_id=new_history_id,
            last_checked_at=now,
        )
    else:
        new_history_id = start_id
        await asyncio.to_thread(update_watch_state, last_checked_at=now)
        if history_failed:
            logger.warning(
                "Deferred history advance: Gmail history API failed; catch-up will retry next tick"
            )
        if failed:
            logger.warning(
                "Deferred history advance: %d message(s) raised errors and will retry",
                failed,
            )

    return {
        "status": "ok",
        "processed": processed,
        "alerts": alerts,
        "failed_retries": failed,
        "history_failed": history_failed,
        "history_id": new_history_id,
    }


async def run_gmail_backfill(bot, days: int = 14) -> dict:
    """One-time scan of recent inbox mail. Does not update watch_state."""
    message_ids = await asyncio.to_thread(list_inbox_message_ids_since, days=days)

    processed = 0
    alerts = 0
    skipped = 0

    for message_id in message_ids:
        processed_state = await _processed_lookup(message_id)
        if processed_state is None:
            logger.warning("Backfill skipping %s — processed state unknown", message_id)
            continue
        if processed_state:
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
            sender=item.get("sender"),
            subject=item.get("subject"),
            header="⏰ <b>Reminder</b>",
        )
        sent_id = None
        if settings.telegram_owner_chat_id:
            sent_id = await send_actionable_alert(
                bot,
                settings.telegram_owner_chat_id,
                text,
                assignment_id=str(assignment_id),
                pin=is_priority_item(
                    category,
                    task=item.get("task"),
                    summary=item.get("summary"),
                    deadline_date=item.get("deadline_date"),
                ),
            )
        if sent_id:
            await asyncio.to_thread(clear_assignment_reminder, str(assignment_id))
            sent += 1

    return {"status": "ok", "reminders_sent": sent}


_gmail_auth_alert_sent = False


def _oauth_start_url() -> str | None:
    settings = get_settings()
    base = (settings.render_url or settings.gmail_oauth_redirect_uri or "").rstrip("/")
    if not base or "your-app" in base:
        return None
    if settings.gmail_oauth_redirect_uri:
        from urllib.parse import urlparse

        parsed = urlparse(settings.gmail_oauth_redirect_uri)
        if parsed.scheme and parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
    import hashlib

    token = hashlib.sha256(f"oauth:{settings.task_secret}".encode()).hexdigest()[:32]
    return f"{base}/oauth/start?token={token}"


async def notify_gmail_auth_failure(bot) -> None:
    """Alert owner once when Gmail refresh token is revoked/expired."""
    global _gmail_auth_alert_sent
    if _gmail_auth_alert_sent:
        return
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    if not owner:
        return

    now = datetime.now(timezone.utc)
    await asyncio.to_thread(update_watch_state, gmail_disconnected_at=now)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    text = (
        "⚠️ <b>Gmail access lost</b>\n\n"
        "Your refresh token expired or was revoked. Mail watching is paused until you reconnect."
    )
    oauth_url = _oauth_start_url()
    markup = None
    if oauth_url:
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Reconnect Gmail", url=oauth_url)]]
        )

    try:
        await bot.send_message(
            chat_id=owner,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        _gmail_auth_alert_sent = True
    except Exception as exc:
        logger.warning("Failed to send Gmail auth failure alert: %s", exc)


async def run_gmail_reconnect_backfill(bot) -> dict:
    """After reconnect, scan outage window and alert on missed + future deadlines."""
    state = await asyncio.to_thread(get_watch_state)
    if not state or not state.get("gmail_disconnected_at"):
        return {"status": "skipped", "reason": "no_outage"}

    disconnected_raw = state["gmail_disconnected_at"]
    try:
        disconnected_at = datetime.fromisoformat(str(disconnected_raw).replace("Z", "+00:00"))
    except ValueError:
        return {"status": "skipped", "reason": "bad_timestamp"}

    days = max(1, min(30, (datetime.now(timezone.utc) - disconnected_at).days + 1))
    result = await run_gmail_backfill(bot, days=days)

    from services.db import get_open_assignments

    missed: list[dict] = []
    now = now_ist()
    items = await asyncio.to_thread(get_open_assignments, limit=100)
    for item in items:
        dt = parse_iso_deadline(item.get("deadline_date"))
        if dt is None:
            continue
        created_raw = item.get("created_at")
        if created_raw:
            try:
                created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                if created >= disconnected_at:
                    continue
            except ValueError:
                pass
        if dt < now:
            missed.append(item)

    settings = get_settings()
    if missed and settings.telegram_owner_chat_id:
        lines = [
            "⚠️ <b>While Gmail was disconnected, you may have missed:</b>",
            "",
        ]
        for item in missed[:15]:
            task = escape_html(str(item.get("task") or item.get("summary") or "Untitled"))
            due = escape_html(format_deadline(item.get("deadline_date"), item.get("deadline_text")))
            lines.append(f"• <b>{task}</b>")
            lines.append(f"   Due: {due}")
        if len(missed) > 15:
            lines.append(f"\n…and {len(missed) - 15} more.")
        await send_html(bot, settings.telegram_owner_chat_id, "\n".join(lines))

    await asyncio.to_thread(
        update_watch_state,
        gmail_reconnected_at=datetime.now(timezone.utc),
        clear_gmail_disconnected=True,
    )
    global _gmail_auth_alert_sent
    _gmail_auth_alert_sent = False
    result["missed_deadlines"] = len(missed)
    result["status"] = "reconnected"
    return result


# Re-export for digest imports
_escape_html = escape_html
_format_deadline_display = format_deadline
CATEGORY_LABELS = CATEGORY_LABELS
