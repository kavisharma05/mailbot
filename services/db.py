import logging
from datetime import date, datetime
from typing import Any

from supabase import Client, create_client

from services.timeutil import parse_iso_deadline, resolve_deadline_text

logger = logging.getLogger(__name__)

_ASSIGNMENT_COLUMNS = (
    "id, category, course_or_topic, task, deadline_text, deadline_date, "
    "summary, confidence, status, created_at"
)


def get_db() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_key)


def get_watch_state() -> dict[str, Any] | None:
    db = get_db()
    result = db.table("watch_state").select("*").eq("id", 1).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


def update_watch_state(last_history_id: str | None = None, last_checked_at: datetime | None = None) -> None:
    db = get_db()
    payload: dict[str, Any] = {}
    if last_history_id is not None:
        payload["last_history_id"] = last_history_id
    if last_checked_at is not None:
        payload["last_checked_at"] = last_checked_at.isoformat()
    if payload:
        db.table("watch_state").update(payload).eq("id", 1).execute()


def is_email_processed(message_id: str) -> bool:
    db = get_db()
    result = db.table("processed_emails").select("message_id").eq("message_id", message_id).limit(1).execute()
    return bool(result.data)


def mark_email_processed(
    message_id: str,
    is_actionable: bool | None,
    confidence: str | None,
    assignment_id: str | None = None,
) -> None:
    db = get_db()
    db.table("processed_emails").insert(
        {
            "message_id": message_id,
            "is_actionable": is_actionable,
            "confidence": confidence,
            "assignment_id": assignment_id,
        }
    ).execute()


def insert_assignment(data: dict[str, Any]) -> dict[str, Any]:
    db = get_db()
    result = db.table("assignments").insert(data).execute()
    return result.data[0]


def get_open_assignments(category: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
    db = get_db()
    query = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .eq("status", "open")
        .order("deadline_date")
        .limit(limit)
    )
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []


def get_assignments_for_qa(category: str | None = None, limit: int = 60) -> list[dict[str, Any]]:
    """Recent tracked items for Q&A — open first, includes recently created items."""
    db = get_db()
    query = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .order("deadline_date")
        .limit(limit)
    )
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []


def _parse_deadline_dt(value: str | None) -> datetime | None:
    return parse_iso_deadline(value)


def _deadline_on_local_day(item: dict[str, Any], day: date, tz) -> bool:
    dt = _parse_deadline_dt(item.get("deadline_date"))
    if dt is None:
        text = item.get("deadline_text")
        if text:
            dt = resolve_deadline_text(text)
    if dt is None:
        return False
    local = dt.astimezone(tz)
    return local.date() == day


def get_assignments_due_on(day: date, tz, category: str | None = None) -> list[dict[str, Any]]:
    """Open assignments whose deadline_date falls on the given local calendar day."""
    items = get_open_assignments(category=category, limit=200)
    return [i for i in items if _deadline_on_local_day(i, day, tz)]


def get_assignments_due_between(
    start_day: date,
    end_day: date,
    tz,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Open assignments with deadline_date on any day from start_day through end_day (inclusive)."""
    items = get_open_assignments(category=category, limit=200)
    matched: list[dict[str, Any]] = []
    for item in items:
        dt = _parse_deadline_dt(item.get("deadline_date"))
        if dt is None:
            dt = resolve_deadline_text(item.get("deadline_text"))
        if dt is None:
            continue
        local_date = dt.astimezone(tz).date()
        if start_day <= local_date <= end_day:
            matched.append(item)
    return matched


def get_recent_assignments(since: datetime, category: str | None = None) -> list[dict[str, Any]]:
    """Assignments created since `since` (typically last 18h for morning digest)."""
    db = get_db()
    query = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .gte("created_at", since.isoformat())
        .order("created_at", desc=True)
        .limit(50)
    )
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []


def mark_assignment_done(assignment_id: str) -> dict[str, Any] | None:
    db = get_db()
    result = (
        db.table("assignments")
        .update({"status": "done"})
        .eq("id", assignment_id)
        .eq("status", "open")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def get_assignment_by_id(assignment_id: str) -> dict[str, Any] | None:
    db = get_db()
    result = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .eq("id", assignment_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def log_low_confidence(kind: str, payload: dict[str, Any]) -> None:
    db = get_db()
    db.table("low_confidence_log").insert({"kind": kind, "payload": payload}).execute()
    logger.info("Logged low-confidence %s output", kind)
