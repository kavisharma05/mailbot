import logging
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar

from supabase import Client, create_client

from config import get_settings
from services.timeutil import item_effective_deadline_dt, parse_iso_deadline

logger = logging.getLogger(__name__)

T = TypeVar("T")

_db_client: Client | None = None

_ASSIGNMENT_COLUMNS = (
    "id, category, course_or_topic, task, deadline_text, deadline_date, "
    "summary, confidence, status, created_at"
)


def get_db() -> Client:
    global _db_client
    if _db_client is None:
        settings = get_settings()
        _db_client = create_client(settings.supabase_url, settings.supabase_service_key)
    return _db_client


def check_db_health() -> dict[str, Any]:
    """Lightweight ping for /healthz — does not raise."""
    try:
        db = get_db()
        result = db.table("watch_state").select("id").eq("id", 1).limit(1).execute()
        if result.data is None:
            return {"ok": False, "error": "empty response"}
        return {"ok": True}
    except Exception as exc:
        logger.warning("DB health check failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _safe_read(default: T) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                logger.exception("%s failed: %s", fn.__name__, exc)
                return default

        return wrapper

    return decorator


def _safe_write(fn: Callable[..., T]) -> Callable[..., T | None]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T | None:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.exception("%s failed: %s", fn.__name__, exc)
            return None

    return wrapper


@_safe_read(None)
def get_watch_state() -> dict[str, Any] | None:
    db = get_db()
    result = db.table("watch_state").select("*").eq("id", 1).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


@_safe_write
def update_watch_state(last_history_id: str | None = None, last_checked_at: datetime | None = None) -> None:
    db = get_db()
    payload: dict[str, Any] = {}
    if last_history_id is not None:
        payload["last_history_id"] = last_history_id
    if last_checked_at is not None:
        payload["last_checked_at"] = last_checked_at.isoformat()
    if payload:
        db.table("watch_state").update(payload).eq("id", 1).execute()


@_safe_read(False)
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
    try:
        db = get_db()
        db.table("processed_emails").insert(
            {
                "message_id": message_id,
                "is_actionable": is_actionable,
                "confidence": confidence,
                "assignment_id": assignment_id,
            }
        ).execute()
    except Exception as exc:
        logger.exception("mark_email_processed failed for %s: %s", message_id, exc)


def insert_assignment(data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        db = get_db()
        result = db.table("assignments").insert(data).execute()
        return result.data[0]
    except Exception as exc:
        logger.exception("insert_assignment failed: %s", exc)
        return None


@_safe_read([])
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


@_safe_read([])
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
    dt = item_effective_deadline_dt(item)
    if dt is None:
        return False
    local = dt.astimezone(tz)
    return local.date() == day


@_safe_read([])
def get_assignments_due_on(day: date, tz, category: str | None = None) -> list[dict[str, Any]]:
    """Open assignments whose deadline_date falls on the given local calendar day."""
    items = get_open_assignments(category=category, limit=200)
    return [i for i in items if _deadline_on_local_day(i, day, tz)]


@_safe_read([])
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
        dt = item_effective_deadline_dt(item)
        if dt is None:
            continue
        local_date = dt.astimezone(tz).date()
        if start_day <= local_date <= end_day:
            matched.append(item)
    return matched


@_safe_read([])
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


@_safe_read(None)
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


@_safe_read(None)
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
    try:
        db = get_db()
        db.table("low_confidence_log").insert({"kind": kind, "payload": payload}).execute()
        logger.info("Logged low-confidence %s output", kind)
    except Exception as exc:
        logger.exception("log_low_confidence failed: %s", exc)


def insert_class_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    try:
        db = get_db()
        result = db.table("class_sessions").insert(rows).execute()
        return result.data or []
    except Exception as exc:
        logger.exception("insert_class_sessions failed: %s", exc)
        return []


@_safe_write
def delete_class_sessions_for_week(week_start: str) -> None:
    db = get_db()
    db.table("class_sessions").delete().eq("week_start", week_start).execute()


@_safe_write
def delete_class_sessions_for_message(gmail_message_id: str) -> None:
    db = get_db()
    db.table("class_sessions").delete().eq("gmail_message_id", gmail_message_id).execute()


@_safe_read([])
def get_classes_on(session_date: str) -> list[dict[str, Any]]:
    db = get_db()
    result = (
        db.table("class_sessions")
        .select(
            "id, course_or_topic, session_date, start_time, end_time, room, faculty, notes, week_start"
        )
        .eq("session_date", session_date)
        .order("start_time")
        .execute()
    )
    return result.data or []


@_safe_read([])
def get_classes_between(start_date: str, end_date: str) -> list[dict[str, Any]]:
    db = get_db()
    result = (
        db.table("class_sessions")
        .select(
            "id, course_or_topic, session_date, start_time, end_time, room, faculty, notes, week_start"
        )
        .gte("session_date", start_date)
        .lte("session_date", end_date)
        .order("session_date")
        .order("start_time")
        .execute()
    )
    return result.data or []


@_safe_read([])
def get_class_sessions_for_message(gmail_message_id: str) -> list[dict[str, Any]]:
    db = get_db()
    result = (
        db.table("class_sessions")
        .select(
            "id, course_or_topic, session_date, start_time, end_time, room, faculty, notes, week_start, gmail_message_id"
        )
        .eq("gmail_message_id", gmail_message_id)
        .order("session_date")
        .order("start_time")
        .execute()
    )
    return result.data or []


@_safe_read(None)
def get_preferred_section() -> str | None:
    result = get_db().table("user_prefs").select("preferred_section").eq("id", 1).execute()
    if result.data:
        return result.data[0].get("preferred_section")
    return None


@_safe_write
def set_preferred_section(section: str) -> None:
    get_db().table("user_prefs").upsert(
        {
            "id": 1,
            "preferred_section": section.strip().upper()[:1],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()
