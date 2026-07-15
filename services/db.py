import logging
from datetime import datetime
from typing import Any

from supabase import Client, create_client

from config import get_settings

logger = logging.getLogger(__name__)


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
        .select(
            "id, category, course_or_topic, task, deadline_text, deadline_date, "
            "summary, confidence, status, created_at"
        )
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
        .select(
            "id, category, course_or_topic, task, deadline_text, deadline_date, "
            "summary, confidence, status, created_at"
        )
        .order("deadline_date")
        .limit(limit)
    )
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []


def log_low_confidence(kind: str, payload: dict[str, Any]) -> None:
    db = get_db()
    db.table("low_confidence_log").insert({"kind": kind, "payload": payload}).execute()
    logger.info("Logged low-confidence %s output", kind)
