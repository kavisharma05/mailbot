import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from services.db import (
    get_assignments_due_between,
    get_assignments_due_on,
    get_digest_state,
    get_preferred_section,
    get_recent_assignments,
    mark_digest_sent,
)
from services.schedule import (
    _format_session_block,
    classes_command_text,
    load_template_sessions,
    refresh_schedule_from_gmail,
    sessions_for_day,
)
from services.formatting import CATEGORY_LABELS, escape_html, format_deadline, is_priority_item
from services.telegram_util import send_html

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_escape_html = escape_html
_format_deadline_display = format_deadline

# Cap long sections so a digest never exceeds Telegram's message limit.
_MAX_OVERDUE_SHOWN = 12
_MAX_DUE_SHOWN = 20


def _priority_items(*lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for items in lists:
        for item in items:
            iid = item.get("id")
            if iid and iid in seen:
                continue
            if iid:
                seen.add(iid)
            if is_priority_item(
                item.get("category") or "other",
                task=item.get("task"),
                summary=item.get("summary"),
            ):
                out.append(item)
    return out


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


def _format_item_line(item: dict[str, Any], *, overdue: bool = False) -> str:
    task = item.get("task") or item.get("summary") or "Untitled"
    course = item.get("course_or_topic") or "—"
    due = _format_deadline_display(item.get("deadline_date"), item.get("deadline_text"))
    cat = CATEGORY_LABELS.get(item.get("category") or "other", "Other")
    prefix = "⚠️ " if overdue else "• "
    return (
        f"{prefix}<b>{_escape_html(str(task))}</b>\n"
        f"   [{_escape_html(cat)}] Course: {_escape_html(str(course))}\n"
        f"   Due: {_escape_html(due)}"
    )


def _format_deadline_section(
    title: str,
    items: list[dict[str, Any]],
    *,
    overdue: bool = False,
    max_shown: int | None = None,
) -> str:
    if not items:
        return ""
    lines = [f"<b>{title} ({len(items)})</b>"]
    shown = items[:max_shown] if max_shown else items
    for item in shown:
        lines.append(_format_item_line(item, overdue=overdue))
        lines.append("")
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append(f"…and {remaining} more.")
    return "\n".join(lines).strip()


def _format_morning_owner_message(
    *,
    today: datetime,
    classes_text: str,
    today_items: list[dict[str, Any]],
    recent_items: list[dict[str, Any]],
    priority_items: list[dict[str, Any]] | None = None,
) -> str:
    date_str = today.strftime("%a, %d %b %Y")
    lines = [f"<b>☀️ Good morning — your day</b>", f"<i>{date_str} (IST)</i>", ""]

    if priority_items:
        lines.append(_format_deadline_section("🔴 Priority", priority_items, max_shown=12))
        lines.append("")

    if classes_text and "📚" in classes_text:
        lines.append(classes_text)
        lines.append("")

    if today_items:
        lines.append(_format_deadline_section("Due today", today_items, max_shown=_MAX_DUE_SHOWN))
        lines.append("")
    else:
        lines.append("<b>Due today</b>: nothing open.")
        lines.append("")

    if recent_items:
        lines.append(f"<b>Recently tracked ({len(recent_items)})</b>")
        for item in recent_items[:8]:
            task = item.get("task") or item.get("summary") or "Untitled"
            due = _format_deadline_display(item.get("deadline_date"), item.get("deadline_text"))
            cat = CATEGORY_LABELS.get(item.get("category") or "other", "Other")
            lines.append(f"• <b>{_escape_html(str(task))}</b> [{_escape_html(cat)}]")
            lines.append(f"   Due: {_escape_html(due)}")
            lines.append("")

    return "\n".join(lines).strip()


def _format_evening_owner_message(
    *,
    tomorrow: datetime,
    classes_text: str,
    overdue_items: list[dict[str, Any]],
    tomorrow_items: list[dict[str, Any]],
    priority_items: list[dict[str, Any]] | None = None,
) -> str:
    date_str = tomorrow.strftime("%a, %d %b %Y")
    lines = [f"<b>🌙 Evening — tomorrow</b>", f"<i>{date_str} (IST)</i>", ""]

    if priority_items:
        lines.append(_format_deadline_section("🔴 Priority", priority_items, max_shown=12))
        lines.append("")

    if overdue_items:
        lines.append(
            _format_deadline_section(
                "Overdue", overdue_items, overdue=True, max_shown=_MAX_OVERDUE_SHOWN
            )
        )
        lines.append("")

    if classes_text and "📚" in classes_text:
        lines.append(classes_text)
        lines.append("")

    if tomorrow_items:
        lines.append(_format_deadline_section("Due tomorrow", tomorrow_items, max_shown=_MAX_DUE_SHOWN))
    elif not classes_text:
        lines.append("Nothing due tomorrow and no classes scheduled yet.")

    return "\n".join(lines).strip()


async def _send_owner_digest(bot, text: str) -> bool:
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    if not owner:
        return False
    return await send_html(bot, owner, text)


def _digest_already_sent_today(kind: str, today: date) -> bool:
    state = get_digest_state() or {}
    key = "last_morning" if kind == "morning" else "last_evening"
    last = state.get(key)
    return last == today


async def _tomorrow_classes_text(tomorrow: date) -> str:
    section = await asyncio.to_thread(get_preferred_section)
    info: dict[str, Any] = {}
    try:
        info = await asyncio.to_thread(refresh_schedule_from_gmail)
    except Exception as exc:
        logger.warning("Evening schedule refresh failed: %s", exc)
    template = load_template_sessions(info if info.get("sessions") else None)
    sessions = sessions_for_day(tomorrow, section=section, template=template)
    if not sessions:
        return ""
    label = tomorrow.strftime("%a, %d %b %Y")
    return _format_session_block(sessions, f"<b>📚 Tomorrow's classes — {label}</b>")


async def run_morning_digest(bot, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    now = datetime.now(IST)
    today = now
    today_date = today.date()

    if not dry_run and not force and _digest_already_sent_today("morning", today_date):
        return {
            "kind": "morning",
            "date_ist": today_date.isoformat(),
            "skipped": "already_sent",
            "dry_run": dry_run,
        }

    since = datetime.now(timezone.utc) - timedelta(hours=18)

    classes_text = ""
    try:
        classes_text = await asyncio.to_thread(classes_command_text, refresh=True)
    except Exception as exc:
        logger.warning("Morning classes refresh failed: %s", exc)

    today_items = await asyncio.to_thread(get_assignments_due_on, today.date(), IST, category=None)
    recent = await asyncio.to_thread(get_recent_assignments, since, category=None)
    today_ids = {i["id"] for i in today_items}
    recent_only = [i for i in recent if i["id"] not in today_ids][:8]
    priority = _priority_items(today_items, recent_only)

    text = _format_morning_owner_message(
        today=today,
        classes_text=classes_text,
        today_items=today_items,
        recent_items=recent_only,
        priority_items=priority,
    )

    sent: list[str] = []
    if not dry_run:
        if await _send_owner_digest(bot, text):
            sent.append("owner")
            await asyncio.to_thread(mark_digest_sent, "morning", today_date)
    else:
        sent.append("preview")

    return {
        "kind": "morning",
        "date_ist": today_date.isoformat(),
        "sent": sent,
        "due_today_count": len(today_items),
        "recent_count": len(recent_only),
        "previews": {"owner": text} if dry_run else {},
        "dry_run": dry_run,
    }


async def run_evening_digest(bot, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    now = datetime.now(IST)
    today = now
    today_date = today.date()
    tomorrow = now + timedelta(days=1)

    if not dry_run and not force and _digest_already_sent_today("evening", today_date):
        return {
            "kind": "evening",
            "date_ist": today_date.isoformat(),
            "tomorrow_ist": tomorrow.date().isoformat(),
            "skipped": "already_sent",
            "dry_run": dry_run,
        }

    classes_text = await _tomorrow_classes_text(tomorrow.date())
    tomorrow_items = await asyncio.to_thread(
        get_assignments_due_on,
        tomorrow.date(),
        IST,
        category=None,
    )

    overdue_items: list[dict[str, Any]] = []
    all_open = await asyncio.to_thread(
        get_assignments_due_between,
        today.date() - timedelta(days=30),
        today.date() - timedelta(days=1),
        IST,
        category=None,
    )
    for item in all_open:
        local_dt = _deadline_local_date(item, IST)
        if local_dt and local_dt.date() < today.date():
            overdue_items.append(item)

    text = _format_evening_owner_message(
        tomorrow=tomorrow,
        classes_text=classes_text,
        overdue_items=overdue_items,
        tomorrow_items=tomorrow_items,
        priority_items=_priority_items(overdue_items, tomorrow_items),
    )

    sent: list[str] = []
    if not dry_run:
        if await _send_owner_digest(bot, text):
            sent.append("owner")
            await asyncio.to_thread(mark_digest_sent, "evening", today_date)
    else:
        sent.append("preview")

    return {
        "kind": "evening",
        "date_ist": today_date.isoformat(),
        "tomorrow_ist": tomorrow.date().isoformat(),
        "sent": sent,
        "due_tomorrow_count": len(tomorrow_items),
        "overdue_count": len(overdue_items),
        "previews": {"owner": text} if dry_run else {},
        "dry_run": dry_run,
    }
