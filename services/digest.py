import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from services.db import (
    acquire_digest_lease,
    digest_already_sent_today,
    get_assignments_due_between,
    get_assignments_due_on,
    get_preferred_section,
    get_recent_assignments,
    mark_digest_sent,
    release_digest_lease,
)
from services.formatting import format_evening_digest, format_morning_digest
from services.schedule import (
    _format_session_block,
    classes_command_text,
    load_template_sessions,
    refresh_schedule_from_gmail,
    sessions_for_day,
)
from services.messaging.pending_digest import deliver_whatsapp_digest
from services.telegram_util import send_html_detailed

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


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


_peer_digest_sent: set[tuple[str, str, str]] = set()


def _peer_digest_users() -> list[dict[str, Any]]:
    """Active students with a Telegram chat. Empty unless the multi-user schema is on."""
    from services.db import multi_user_schema_ready

    if not multi_user_schema_ready():
        return []
    try:
        from services.users import is_owner_user, list_users
    except Exception:
        return []
    peers: list[dict[str, Any]] = []
    for user in list_users(active_only=True):
        if is_owner_user(user):
            continue
        if not str(user.get("telegram_chat_id") or "").strip():
            continue
        peers.append(user)
    return peers


async def _send_peer_digests(bot, *, kind: str, day: date, build_text) -> list[str]:
    """Send each student their own briefing. WhatsApp stays on the owner phone."""
    from services import mailbox_user

    sent: list[str] = []
    for user in _peer_digest_users():
        uid = str(user["id"])
        memory_key = (uid, kind, day.isoformat())
        if memory_key in _peer_digest_sent:
            continue
        if digest_already_sent_today(kind, day, user_id=uid):
            _peer_digest_sent.add(memory_key)
            continue
        if not acquire_digest_lease(kind, day, user_id=uid):
            continue
        token = mailbox_user.set_user(user)
        try:
            text = await build_text(uid)
            chat = str(user.get("telegram_chat_id") or "")
            result = await send_html_detailed(bot, chat, text)
        except Exception as exc:
            logger.warning("Peer %s digest failed for %s: %s", kind, uid, exc)
            release_digest_lease(kind, day, user_id=uid)
            mailbox_user.reset(token)
            continue
        mailbox_user.reset(token)
        if result.ok:
            _peer_digest_sent.add(memory_key)
            mark_digest_sent(kind, day, user_id=uid)
            sent.append(uid)
        else:
            release_digest_lease(kind, day, user_id=uid)
    return sent


async def _send_owner_digest(bot, text: str, *, kind: str = "morning") -> bool:
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    if not owner:
        return False
    result = await send_html_detailed(bot, owner, text)
    try:
        await deliver_whatsapp_digest(text, kind=kind)
    except Exception as exc:
        logger.warning("WhatsApp digest deliver failed: %s", exc)
    return result.ok


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


async def _morning_text(today: datetime, uid: str) -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=18)
    classes_text = ""
    try:
        classes_text = await asyncio.to_thread(classes_command_text, refresh=False)
    except Exception as exc:
        logger.warning("Peer morning classes failed: %s", exc)
    today_items = await asyncio.to_thread(get_assignments_due_on, today.date(), IST, None, uid)
    recent = await asyncio.to_thread(get_recent_assignments, since, None, uid)
    today_ids = {i["id"] for i in today_items}
    recent_only = [i for i in recent if i["id"] not in today_ids][:8]
    return format_morning_digest(
        today=today,
        classes_text=classes_text,
        today_items=today_items,
        recent_items=recent_only,
    )


async def _evening_text(today: datetime, tomorrow: datetime, uid: str) -> str:
    classes_text = await _tomorrow_classes_text(tomorrow.date())
    tomorrow_items = await asyncio.to_thread(
        get_assignments_due_on, tomorrow.date(), IST, None, uid
    )
    overdue_items: list[dict[str, Any]] = []
    older_open_count = 0
    all_open = await asyncio.to_thread(
        get_assignments_due_between,
        today.date() - timedelta(days=45),
        today.date() - timedelta(days=1),
        IST,
        None,
        uid,
    )
    for item in all_open:
        local_dt = _deadline_local_date(item, IST)
        if not local_dt or local_dt.date() >= today.date():
            continue
        age_days = (today.date() - local_dt.date()).days
        if age_days <= 7:
            overdue_items.append(item)
        else:
            older_open_count += 1
    return format_evening_digest(
        tomorrow=tomorrow,
        classes_text=classes_text,
        overdue_items=overdue_items,
        tomorrow_items=tomorrow_items,
        older_open_count=older_open_count,
    )


async def run_morning_digest(bot, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    now = datetime.now(IST)
    today = now
    today_date = today.date()

    if not dry_run and not force and digest_already_sent_today("morning", today_date):
        await _send_peer_digests(
            bot, kind="morning", day=today_date, build_text=lambda uid: _morning_text(today, uid)
        )
        return {
            "kind": "morning",
            "date_ist": today_date.isoformat(),
            "skipped": "already_sent",
            "dry_run": dry_run,
        }

    lease_acquired = False
    if not dry_run and not force:
        lease_acquired = await asyncio.to_thread(acquire_digest_lease, "morning", today_date)
        if not lease_acquired:
            await _send_peer_digests(
                bot, kind="morning", day=today_date, build_text=lambda uid: _morning_text(today, uid)
            )
            return {
                "kind": "morning",
                "date_ist": today_date.isoformat(),
                "skipped": "lease_unavailable",
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

    text = format_morning_digest(
        today=today,
        classes_text=classes_text,
        today_items=today_items,
        recent_items=recent_only,
    )

    sent: list[str] = []
    if not dry_run:
        if await _send_owner_digest(bot, text, kind="morning"):
            sent.append("owner")
            await asyncio.to_thread(mark_digest_sent, "morning", today_date)
        elif lease_acquired:
            await asyncio.to_thread(release_digest_lease, "morning", today_date)
    else:
        sent.append("preview")

    if not dry_run:
        sent.extend(
            await _send_peer_digests(
                bot, kind="morning", day=today_date, build_text=lambda uid: _morning_text(today, uid)
            )
        )

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

    if not dry_run and not force and digest_already_sent_today("evening", today_date):
        await _send_peer_digests(
            bot,
            kind="evening",
            day=today_date,
            build_text=lambda uid: _evening_text(today, tomorrow, uid),
        )
        return {
            "kind": "evening",
            "date_ist": today_date.isoformat(),
            "tomorrow_ist": tomorrow.date().isoformat(),
            "skipped": "already_sent",
            "dry_run": dry_run,
        }

    lease_acquired = False
    if not dry_run and not force:
        lease_acquired = await asyncio.to_thread(acquire_digest_lease, "evening", today_date)
        if not lease_acquired:
            await _send_peer_digests(
                bot,
                kind="evening",
                day=today_date,
                build_text=lambda uid: _evening_text(today, tomorrow, uid),
            )
            return {
                "kind": "evening",
                "date_ist": today_date.isoformat(),
                "tomorrow_ist": tomorrow.date().isoformat(),
                "skipped": "lease_unavailable",
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
    older_open_count = 0
    all_open = await asyncio.to_thread(
        get_assignments_due_between,
        today.date() - timedelta(days=45),
        today.date() - timedelta(days=1),
        IST,
        category=None,
    )
    for item in all_open:
        local_dt = _deadline_local_date(item, IST)
        if not local_dt or local_dt.date() >= today.date():
            continue
        age_days = (today.date() - local_dt.date()).days
        if age_days <= 7:
            overdue_items.append(item)
        else:
            older_open_count += 1

    text = format_evening_digest(
        tomorrow=tomorrow,
        classes_text=classes_text,
        overdue_items=overdue_items,
        tomorrow_items=tomorrow_items,
        older_open_count=older_open_count,
    )

    sent: list[str] = []
    if not dry_run:
        if await _send_owner_digest(bot, text, kind="evening"):
            sent.append("owner")
            await asyncio.to_thread(mark_digest_sent, "evening", today_date)
        elif lease_acquired:
            await asyncio.to_thread(release_digest_lease, "evening", today_date)
    else:
        sent.append("preview")

    if not dry_run:
        sent.extend(
            await _send_peer_digests(
                bot,
                kind="evening",
                day=today_date,
                build_text=lambda uid: _evening_text(today, tomorrow, uid),
            )
        )

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
