import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import Settings, get_settings
from services.db import (
    get_assignments_due_between,
    get_assignments_due_on,
    get_recent_assignments,
)
from services.schedule import classes_command_text
from services.watcher import CATEGORY_LABELS, VALID_CATEGORIES, _escape_html, _format_deadline_display

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _resolve_chat_id(category: str, settings: Settings) -> str:
    mapping = {
        "deadline": settings.telegram_chat_deadlines,
        "placement": settings.telegram_chat_placements,
        "admin": settings.telegram_chat_admin,
        "other": settings.telegram_chat_other,
    }
    return mapping.get(category, "") or settings.telegram_owner_chat_id


def _deadline_local_date(item: dict[str, Any], tz) -> datetime | None:
    raw = item.get("deadline_date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _format_item_line(item: dict[str, Any], tz, *, overdue: bool = False) -> str:
    task = item.get("task") or item.get("summary") or "Untitled"
    course = item.get("course_or_topic") or "—"
    due = _format_deadline_display(item.get("deadline_date"), item.get("deadline_text"))
    prefix = "⚠️ " if overdue else "• "
    return (
        f"{prefix}<b>{_escape_html(str(task))}</b>\n"
        f"   Course: {_escape_html(str(course))}\n"
        f"   Due: {_escape_html(due)}"
    )


def _format_morning_message(
    *,
    today_items: list[dict[str, Any]],
    recent_items: list[dict[str, Any]],
    category: str,
    today: datetime,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    date_str = today.strftime("%a, %d %b %Y")
    lines = [f"<b>☀️ Morning digest — {label}</b>", f"<i>{date_str} (IST)</i>", ""]

    if today_items:
        lines.append(f"<b>Due today ({len(today_items)})</b>")
        for item in today_items:
            lines.append(_format_item_line(item, IST))
            lines.append("")
    else:
        lines.append("<b>Due today</b>: nothing open.")
        lines.append("")

    if recent_items:
        lines.append(f"<b>Latest tracked ({len(recent_items)})</b>")
        for item in recent_items[:10]:
            task = item.get("task") or item.get("summary") or "Untitled"
            due = _format_deadline_display(item.get("deadline_date"), item.get("deadline_text"))
            lines.append(f"• <b>{_escape_html(str(task))}</b>")
            lines.append(f"   Due: {_escape_html(due)}")
            lines.append("")

    return "\n".join(lines).strip()


def _format_evening_message(
    *,
    items: list[dict[str, Any]],
    category: str,
    today: datetime,
    tomorrow: datetime,
) -> str:
    label = CATEGORY_LABELS.get(category, "Mail")
    lines = [
        f"<b>🌙 Evening reminder — {label}</b>",
        f"<i>{today.strftime('%a, %d %b')} – {tomorrow.strftime('%a, %d %b %Y')} (IST)</i>",
        "",
    ]

    if not items:
        lines.append("Nothing due today or tomorrow.")
        return "\n".join(lines)

    today_date = today.date()
    overdue: list[dict[str, Any]] = []
    due_today: list[dict[str, Any]] = []
    due_tomorrow: list[dict[str, Any]] = []

    for item in items:
        local_dt = _deadline_local_date(item, IST)
        if local_dt is None:
            continue
        local_day = local_dt.date()
        if local_day < today_date:
            overdue.append(item)
        elif local_day == today_date:
            due_today.append(item)
        elif local_day == tomorrow.date():
            due_tomorrow.append(item)

    if overdue:
        lines.append(f"<b>Overdue ({len(overdue)})</b>")
        for item in overdue:
            lines.append(_format_item_line(item, IST, overdue=True))
            lines.append("")

    if due_today:
        lines.append(f"<b>Due today ({len(due_today)})</b>")
        for item in due_today:
            lines.append(_format_item_line(item, IST))
            lines.append("")

    if due_tomorrow:
        lines.append(f"<b>Due tomorrow ({len(due_tomorrow)})</b>")
        for item in due_tomorrow:
            lines.append(_format_item_line(item, IST))
            lines.append("")

    return "\n".join(lines).strip()


async def _send_category_digest(bot, category: str, text: str) -> bool:
    settings = get_settings()
    chat_id = _resolve_chat_id(category, settings)
    if not chat_id:
        return False
    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    return True


async def run_morning_digest(bot, *, dry_run: bool = False) -> dict[str, Any]:
    now = datetime.now(IST)
    today = now
    since = datetime.now(timezone.utc) - timedelta(hours=18)

    sent: list[str] = []
    skipped: list[str] = []
    previews: dict[str, str] = {}

    # Today's classes — refresh from latest timetable mail/PDF, then send
    try:
        classes_text = classes_command_text(refresh=True)
    except Exception as exc:
        logger.warning("Morning classes refresh failed: %s", exc)
        classes_text = ""
    # Digest only sends a real schedule list, not the soft error copy from /classes
    if classes_text and "📚" in classes_text:
        previews["classes"] = classes_text
        if not dry_run:
            settings = get_settings()
            class_chats: list[str] = []
            for chat_id in (
                settings.telegram_chat_deadlines or "",
                settings.telegram_owner_chat_id,
            ):
                if chat_id and chat_id not in class_chats:
                    class_chats.append(chat_id)
            for chat_id in class_chats:
                try:
                    await bot.send_message(chat_id=chat_id, text=classes_text, parse_mode="HTML")
                except Exception as exc:
                    logger.warning("Failed sending today's classes to %s: %s", chat_id, exc)
            sent.append("classes")

    for category in VALID_CATEGORIES:
        today_items = get_assignments_due_on(today.date(), IST, category=category)
        recent = get_recent_assignments(since, category=category)
        # Avoid duplicating today's due items in recent section
        today_ids = {i["id"] for i in today_items}
        recent_only = [i for i in recent if i["id"] not in today_ids]

        if not today_items and not recent_only:
            skipped.append(category)
            continue

        text = _format_morning_message(
            today_items=today_items,
            recent_items=recent_only,
            category=category,
            today=today,
        )
        previews[category] = text

        if not dry_run:
            ok = await _send_category_digest(bot, category, text)
            if ok:
                sent.append(category)
            else:
                skipped.append(category)

    if not dry_run and not sent:
        settings = get_settings()
        owner = settings.telegram_owner_chat_id
        if owner:
            await bot.send_message(
                chat_id=owner,
                text="☀️ <b>Morning digest</b>\n\nNothing due today and no new tracked items.",
                parse_mode="HTML",
            )
            sent.append("owner_fallback")

    return {
        "kind": "morning",
        "date_ist": today.date().isoformat(),
        "sent": sent,
        "skipped": skipped,
        "previews": previews if dry_run else {},
        "dry_run": dry_run,
    }


async def run_evening_digest(bot, *, dry_run: bool = False) -> dict[str, Any]:
    now = datetime.now(IST)
    today = now
    tomorrow = now + timedelta(days=1)

    sent: list[str] = []
    skipped: list[str] = []
    previews: dict[str, str] = {}

    for category in VALID_CATEGORIES:
        items = get_assignments_due_between(today.date(), tomorrow.date(), IST, category=category)
        # Also include overdue open items for this category
        all_open = get_assignments_due_between(
            today.date() - timedelta(days=30),
            tomorrow.date(),
            IST,
            category=category,
        )
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in all_open:
            iid = item.get("id")
            if iid and iid not in seen:
                seen.add(iid)
                merged.append(item)

        overdue_count = sum(
            1
            for i in merged
            if (d := _deadline_local_date(i, IST)) and d.date() < today.date()
        )
        has_content = bool(merged) and (len(items) > 0 or overdue_count > 0)

        if not has_content:
            skipped.append(category)
            continue

        text = _format_evening_message(
            items=merged,
            category=category,
            today=today,
            tomorrow=tomorrow,
        )
        previews[category] = text

        if not dry_run:
            ok = await _send_category_digest(bot, category, text)
            if ok:
                sent.append(category)
            else:
                skipped.append(category)

    if not dry_run and not sent:
        settings = get_settings()
        owner = settings.telegram_owner_chat_id
        if owner:
            await bot.send_message(
                chat_id=owner,
                text="🌙 <b>Evening reminder</b>\n\nNothing due today or tomorrow.",
                parse_mode="HTML",
            )
            sent.append("owner_fallback")

    return {
        "kind": "evening",
        "date_ist": today.date().isoformat(),
        "sent": sent,
        "skipped": skipped,
        "previews": previews if dry_run else {},
        "dry_run": dry_run,
    }
