"""Deadline-relative scheduled reminders (T-24h, T-3h, etc.)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from services.alert_delivery import send_actionable_alert
from services.db import (
    cancel_scheduled_reminders,
    get_assignment_by_id,
    get_due_scheduled_reminders,
    insert_scheduled_reminders,
    mark_scheduled_reminder_sent,
)
from services.formatting import category_icon, escape_html, format_deadline, is_priority_item, normalize_category
from services.timeutil import now_ist, parse_iso_deadline

logger = logging.getLogger(__name__)

TIER_DELTAS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "3h": timedelta(hours=3),
    "1h": timedelta(hours=1),
    "30m": timedelta(minutes=30),
}

TIER_LABELS = {
    "24h": "24 hours",
    "3h": "3 hours",
    "1h": "1 hour",
    "30m": "30 minutes",
}

PRIORITY_TIERS: dict[str, tuple[str, ...]] = {
    "high": ("24h", "3h", "1h", "30m"),
    "normal": ("24h", "3h"),
    "low": ("3h",),
}


def _tiers_for_assignment(assignment: dict[str, Any]) -> tuple[str, ...]:
    priority = (assignment.get("priority") or "normal").lower()
    if priority not in PRIORITY_TIERS:
        priority = "normal"
    if is_priority_item(
        assignment.get("category") or "other",
        task=assignment.get("task"),
        summary=assignment.get("summary"),
        deadline_date=assignment.get("deadline_date"),
    ):
        return PRIORITY_TIERS["high"]
    return PRIORITY_TIERS.get(priority, PRIORITY_TIERS["normal"])


def schedule_deadline_reminders(assignment: dict[str, Any]) -> int:
    """Create pending scheduled_reminders rows for an assignment with a deadline."""
    assignment_id = assignment.get("id")
    if not assignment_id:
        return 0

    deadline = parse_iso_deadline(assignment.get("deadline_date"))
    if deadline is None:
        return 0

    now = now_ist()
    rows: list[dict[str, Any]] = []
    for tier in _tiers_for_assignment(assignment):
        delta = TIER_DELTAS.get(tier)
        if delta is None:
            continue
        fire_at = deadline - delta
        if fire_at <= now:
            continue
        rows.append(
            {
                "assignment_id": str(assignment_id),
                "fire_at": fire_at.astimezone(timezone.utc).isoformat(),
                "tier": tier,
                "status": "pending",
            }
        )

    if not rows:
        return 0
    inserted = insert_scheduled_reminders(rows)
    logger.info("Scheduled %d reminder(s) for assignment %s", len(inserted), assignment_id)
    return len(inserted)


def _format_reminder_message(item: dict[str, Any], tier: str) -> str:
    category = normalize_category(item.get("category"))
    icon = category_icon(category)
    task = item.get("task") or item.get("summary") or "Untitled"
    course = item.get("course_or_topic") or "—"
    due = format_deadline(item.get("deadline_date"), item.get("deadline_text"))
    tier_label = TIER_LABELS.get(tier, tier)
    lines = [
        f"⏰ <b>Reminder — due in {tier_label}</b>",
        "",
        f"<b>Task:</b> {escape_html(str(task))}",
        f"<b>Course:</b> {escape_html(str(course))}",
        f"<b>Due:</b> {escape_html(due)}",
        f"<b>Category:</b> {icon} {escape_html(category.title())}",
    ]
    if item.get("summary"):
        lines.extend(["", escape_html(str(item["summary"]))])
    return "\n".join(lines)


async def run_scheduled_reminders(bot) -> dict[str, Any]:
    """Fire due scheduled reminders to owner DM."""
    from config import get_settings

    due_rows = await asyncio.to_thread(get_due_scheduled_reminders)
    sent = 0
    settings = get_settings()
    owner = settings.telegram_owner_chat_id

    for row in due_rows:
        reminder_id = row.get("id")
        assignment_id = row.get("assignment_id")
        tier = row.get("tier") or "3h"
        if not reminder_id or not assignment_id:
            continue

        item = await asyncio.to_thread(get_assignment_by_id, str(assignment_id))
        if not item or item.get("status") != "open":
            await asyncio.to_thread(cancel_scheduled_reminders, str(assignment_id))
            await asyncio.to_thread(mark_scheduled_reminder_sent, str(reminder_id))
            continue

        text = _format_reminder_message(item, tier)
        category = normalize_category(item.get("category"))
        msg_id = None
        if owner:
            msg_id = await send_actionable_alert(
                bot,
                owner,
                text,
                assignment_id=str(assignment_id),
                pin=is_priority_item(
                    category,
                    task=item.get("task"),
                    summary=item.get("summary"),
                    deadline_date=item.get("deadline_date"),
                ),
            )
        if msg_id:
            await asyncio.to_thread(mark_scheduled_reminder_sent, str(reminder_id))
            sent += 1

    return {"status": "ok", "reminders_sent": sent, "due_count": len(due_rows)}
