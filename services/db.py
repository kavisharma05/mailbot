import logging
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from supabase import Client, create_client

from config import get_settings
from services.timeutil import item_effective_deadline_dt, parse_iso_deadline

logger = logging.getLogger(__name__)

T = TypeVar("T")

_db_client: Client | None = None

_ASSIGNMENT_COLUMNS = (
    "id, category, course_or_topic, task, deadline_text, deadline_date, "
    "summary, confidence, status, remind_at, gmail_message_id, gmail_thread_id, created_at"
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


# Short TTL cache: when Supabase flakes, avoid reprocessing storms (returns True = skip)
_processed_cache: dict[str, float] = {}
_PROCESSED_CACHE_TTL = 120.0
_PROCESSED_CACHE_MAX_ENTRIES = 2000


def _prune_processed_cache(now: float | None = None) -> None:
    """Drop expired entries and cap size (mirrors telegram_dedupe)."""
    import time

    now = now if now is not None else time.time()
    expired = [mid for mid, expiry in _processed_cache.items() if expiry <= now]
    for mid in expired:
        _processed_cache.pop(mid, None)
    while len(_processed_cache) > _PROCESSED_CACHE_MAX_ENTRIES:
        _processed_cache.pop(next(iter(_processed_cache)))


def is_email_processed_cached(message_id: str) -> bool:
    import time

    now = time.time()
    _prune_processed_cache(now)
    cached = _processed_cache.get(message_id)
    if cached and cached > now:
        return True
    if is_email_processed(message_id):
        _processed_cache[message_id] = now + _PROCESSED_CACHE_TTL
        _prune_processed_cache(now)
        return True
    return False


def cache_email_processed(message_id: str) -> None:
    import time

    now = time.time()
    _processed_cache[message_id] = now + _PROCESSED_CACHE_TTL
    _prune_processed_cache(now)


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
        cache_email_processed(message_id)
    except Exception as exc:
        logger.exception("mark_email_processed failed for %s: %s", message_id, exc)


@_safe_read(None)
def find_open_assignment_for_message(message_id: str) -> dict[str, Any] | None:
    db = get_db()
    result = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .eq("gmail_message_id", message_id)
        .eq("status", "open")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def insert_assignment(data: dict[str, Any]) -> dict[str, Any] | None:
    message_id = data.get("gmail_message_id")
    if message_id:
        existing = find_open_assignment_for_message(str(message_id))
        if existing:
            logger.info("Deduped assignment for message %s -> %s", message_id, existing["id"])
            return existing
    try:
        db = get_db()
        result = db.table("assignments").insert(data).execute()
        rows = result.data or []
        if not rows:
            logger.error(
                "insert_assignment returned no rows for message %s (task=%s)",
                message_id,
                data.get("task"),
            )
            return None
        return rows[0]
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
        .update({"status": "done", "remind_at": None})
        .eq("id", assignment_id)
        .eq("status", "open")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


@_safe_read(None)
def dismiss_assignment(assignment_id: str) -> dict[str, Any] | None:
    db = get_db()
    result = (
        db.table("assignments")
        .update({"status": "dismissed", "remind_at": None})
        .eq("id", assignment_id)
        .eq("status", "open")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


@_safe_read(None)
def schedule_assignment_reminder(assignment_id: str, remind_at: datetime) -> dict[str, Any] | None:
    db = get_db()
    result = (
        db.table("assignments")
        .update({"remind_at": remind_at.isoformat()})
        .eq("id", assignment_id)
        .eq("status", "open")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


@_safe_read([])
def get_assignments_due_reminder(before: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
    db = get_db()
    cutoff = (before or datetime.now(timezone.utc)).isoformat()
    result = (
        db.table("assignments")
        .select(_ASSIGNMENT_COLUMNS)
        .eq("status", "open")
        .not_.is_("remind_at", "null")
        .lte("remind_at", cutoff)
        .order("remind_at")
        .limit(limit)
        .execute()
    )
    return result.data or []


@_safe_read(None)
def clear_assignment_reminder(assignment_id: str) -> None:
    db = get_db()
    db.table("assignments").update({"remind_at": None}).eq("id", assignment_id).execute()


def enqueue_pending_email(message_id: str, error: str | None = None) -> None:
    try:
        db = get_db()
        existing = (
            db.table("pending_emails")
            .select("message_id, attempts")
            .eq("message_id", message_id)
            .limit(1)
            .execute()
        )
        rows = existing.data or []
        if rows:
            db.table("pending_emails").update(
                {
                    "attempts": int(rows[0].get("attempts") or 0) + 1,
                    "last_error": error,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("message_id", message_id).execute()
        else:
            db.table("pending_emails").insert(
                {
                    "message_id": message_id,
                    "attempts": 1,
                    "last_error": error,
                }
            ).execute()
    except Exception as exc:
        logger.exception("enqueue_pending_email failed for %s: %s", message_id, exc)


def dequeue_pending_email(message_id: str) -> None:
    try:
        db = get_db()
        db.table("pending_emails").delete().eq("message_id", message_id).execute()
    except Exception as exc:
        logger.exception("dequeue_pending_email failed for %s: %s", message_id, exc)


@_safe_read([])
def list_pending_email_ids(limit: int = 100) -> list[str]:
    db = get_db()
    result = (
        db.table("pending_emails")
        .select("message_id")
        .order("updated_at")
        .limit(limit)
        .execute()
    )
    return [row["message_id"] for row in (result.data or []) if row.get("message_id")]


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


@_safe_write
def upsert_contact(email: str, display_name: str | None, name_key: str) -> None:
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    existing = db.table("contacts").select("id, message_count").eq("email", email).limit(1).execute()
    rows = existing.data or []
    if rows:
        count = int(rows[0].get("message_count") or 0) + 1
        db.table("contacts").update(
            {
                "display_name": display_name or rows[0].get("display_name"),
                "name_key": name_key or rows[0].get("name_key") or "",
                "message_count": count,
                "last_seen_at": now,
            }
        ).eq("email", email).execute()
        return
    db.table("contacts").insert(
        {
            "email": email,
            "display_name": display_name,
            "name_key": name_key or "",
            "message_count": 1,
            "last_seen_at": now,
        }
    ).execute()


@_safe_read([])
def find_contacts_by_name(token: str, limit: int = 20) -> list[dict[str, Any]]:
    """Find contacts whose name_key/display_name contains the token."""
    if not token or len(token) < 2:
        return []
    db = get_db()
    needle = token.lower()
    result = (
        db.table("contacts")
        .select("email, display_name, name_key, message_count, last_seen_at")
        .or_(f"name_key.ilike.%{needle}%,display_name.ilike.%{needle}%,email.ilike.%{needle}%")
        .order("message_count", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# --- Digest idempotency (in-memory fallback if migration not applied yet) ---

_digest_state_fallback: dict[str, date | None] = {"last_morning": None, "last_evening": None}
_digest_state_table_missing = False


def get_digest_state() -> dict[str, date | None]:
    global _digest_state_table_missing
    if _digest_state_table_missing:
        return dict(_digest_state_fallback)
    try:
        db = get_db()
        result = (
            db.table("digest_state")
            .select("last_morning, last_evening")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        err = str(exc).lower()
        if "digest_state" in err or "does not exist" in err or "42p01" in err:
            _digest_state_table_missing = True
            logger.warning("digest_state table missing — using in-memory fallback: %s", exc)
            return dict(_digest_state_fallback)
        logger.exception("get_digest_state failed: %s", exc)
        return dict(_digest_state_fallback)

    rows = result.data or []
    if not rows:
        return {"last_morning": None, "last_evening": None}

    def _parse_day(raw: Any) -> date | None:
        if not raw:
            return None
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None

    row = rows[0]
    return {
        "last_morning": _parse_day(row.get("last_morning")),
        "last_evening": _parse_day(row.get("last_evening")),
    }


def claim_digest(kind: str, sent_on: date | None = None) -> bool:
    """Atomically claim today's digest slot. Returns True if this caller should send."""
    global _digest_state_table_missing
    if kind not in ("morning", "evening"):
        return False

    day = sent_on or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    column = "last_morning" if kind == "morning" else "last_evening"

    if _digest_state_table_missing:
        if _digest_state_fallback.get(column) == day:
            return False
        _digest_state_fallback[column] = day
        return True

    try:
        db = get_db()
        result = (
            db.table("digest_state")
            .update(
                {
                    column: day.isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("id", 1)
            .or_(f"{column}.is.null,{column}.neq.{day.isoformat()}")
            .execute()
        )
        rows = result.data or []
        if rows:
            _digest_state_fallback[column] = day
            return True
        return False
    except Exception as exc:
        err = str(exc).lower()
        if "digest_state" in err or "does not exist" in err or "42p01" in err:
            _digest_state_table_missing = True
            logger.warning("digest_state table missing — using in-memory fallback: %s", exc)
            if _digest_state_fallback.get(column) == day:
                return False
            _digest_state_fallback[column] = day
            return True
        logger.exception("claim_digest failed for %s: %s", kind, exc)
        return False


def mark_digest_sent(kind: str, sent_on: date | None = None) -> None:
    """Record that morning or evening digest was sent on sent_on (IST calendar day)."""
    global _digest_state_table_missing
    if kind not in ("morning", "evening"):
        return
    day = sent_on or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    column = "last_morning" if kind == "morning" else "last_evening"
    _digest_state_fallback[column] = day

    if _digest_state_table_missing:
        return
    try:
        db = get_db()
        db.table("digest_state").update(
            {column: day.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", 1).execute()
    except Exception as exc:
        err = str(exc).lower()
        if "digest_state" in err or "does not exist" in err or "42p01" in err:
            _digest_state_table_missing = True
            logger.warning("digest_state table missing — using in-memory fallback: %s", exc)
        else:
            logger.exception("mark_digest_sent failed for %s: %s", kind, exc)
