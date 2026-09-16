"""REST API for the Mailbot mobile web client."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ai.assistant import answer_question
from ai.composer import compose_email
from ai.intent import INTENT_COMPOSE, classify_message
from config import get_settings
from gmail_api.client import send_email
from services.contacts import resolve_contact_email
from services.db import (
    check_db_health,
    create_manual_assignment,
    dismiss_assignment,
    get_assignment_by_id,
    get_assignments_for_qa,
    get_db,
    get_missed_assignments,
    get_open_assignments,
    get_preferred_section,
    get_undated_assignments,
    get_upcoming_assignments,
    get_watch_state,
    mark_assignment_done,
    schedule_assignment_reminder,
    update_assignment,
)
from services.schedule import (
    refresh_schedule_from_gmail,
    schedule_cache_is_fresh,
    sessions_for_day,
)
from ai.deadline import correct_category_from_text
from services.timeutil import IST, format_deadline_display, item_effective_deadline_dt, now_ist, today_ist
from services.web_conversation import (
    chat_history_for_api,
    clear_web_convo,
    get_web_compose,
    get_web_convo,
    remember_web_turn,
    set_web_compose,
)
from services.websocket_manager import broadcast

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["web"])

_ASSIGNMENT_WEB_COLUMNS = (
    "id, source, category, item_type, priority, course_or_topic, task, deadline_text, deadline_date, "
    "summary, confidence, status, remind_at, gmail_message_id, gmail_thread_id, created_at, sender, subject"
)


def _strip_when(text: str) -> str:
    text = re.sub(r"\s*[—–-]\s*due\b.*", "", text, flags=re.I)
    text = re.sub(r"\s+by\s+\d{1,2}\s+\w+(\s+\d{4})?.*$", "", text, flags=re.I)
    return text.strip()


def _specific_title(item: dict[str, Any], title: str) -> str:
    """Turn vague titles like 'Complete registration' into the actual item."""
    raw = title.strip()
    course = (item.get("course_or_topic") or "").strip()
    summary = _strip_when(item.get("summary") or "")
    generic = bool(
        re.fullmatch(
            r"(complete|submit|finish|do|fill)?\s*(the\s+)?(registration|form|task)",
            raw,
            flags=re.I,
        )
    )
    if generic and course:
        if re.search(r"registration", raw, flags=re.I):
            return f"Complete {course} registration"[:90]
        if re.search(r"form", raw, flags=re.I):
            return f"Complete {course} form"[:90]
        return f"{raw} — {course}"[:90]
    if generic and summary and len(summary) > len(raw) + 3:
        return summary[:90]
    return raw


def _short_task_detail(item: dict[str, Any], *, title: str) -> str:
    """One-line context that is not just a repeat of the task title."""
    title_l = title.lower().strip()
    course = (item.get("course_or_topic") or "").strip()
    if course and course.lower() not in title_l:
        return course[:140]

    summary = _strip_when(item.get("summary") or "")
    if summary:
        summary_l = summary.lower()
        if summary_l != title_l and summary_l not in title_l and title_l not in summary_l:
            if title_l and summary_l.startswith(title_l):
                rest = summary[len(title) :].lstrip(" .,—–-:;")
                if rest:
                    return rest[:140]
            if len(summary) > 8:
                return summary[:140]
    subject = (item.get("subject") or "").strip()
    if subject and subject.lower() not in title_l:
        return subject[:140]
    return ""


def _serialize_task(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    ref = now or now_ist()
    dt = item_effective_deadline_dt(item)
    status_filter = "all"
    if item.get("status") == "done":
        status_filter = "done"
    elif dt is None:
        status_filter = "undated"
    elif dt < ref:
        status_filter = "missed"
    else:
        status_filter = "due-soon"
    title = item.get("task") or item.get("summary") or ""
    display = _specific_title(item, str(title))
    category = correct_category_from_text(
        item.get("category"),
        item.get("task"),
        item.get("summary"),
        item.get("subject"),
        item.get("course_or_topic"),
    )
    return {
        "id": item.get("id"),
        "task": display,
        "summary": item.get("summary") or "",
        "detail": _short_task_detail(item, title=display),
        "category": category,
        "deadline_text": item.get("deadline_text"),
        "deadline_date": item.get("deadline_date"),
        "deadline_display": format_deadline_display(
            item.get("deadline_date"), item.get("deadline_text")
        ),
        "status": item.get("status") or "open",
        "status_filter": status_filter,
        "priority": item.get("priority") or "normal",
        "source": item.get("source") or "email",
        "sender": item.get("sender"),
        "subject": item.get("subject"),
        "course_or_topic": item.get("course_or_topic"),
        "remind_at": item.get("remind_at"),
        "created_at": item.get("created_at"),
    }


def _serialize_class(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session.get("id"),
        "course": session.get("course_or_topic") or "Class",
        "start_time": session.get("start_time"),
        "end_time": session.get("end_time"),
        "room": session.get("room"),
        "faculty": session.get("faculty"),
        "notes": session.get("notes"),
        "session_date": session.get("session_date"),
        "cancelled": bool(session.get("cancelled")),
        "change": session.get("change"),
    }


def _greeting_name() -> str:
    settings = get_settings()
    name = (settings.student_name or "Student").strip()
    if name.startswith("["):
        return "there"
    return name.split()[0] if name else "there"


def _sync_meta() -> dict[str, Any]:
    state = get_watch_state() or {}
    last = state.get("last_checked_at") or state.get("last_heartbeat_at")
    return {
        "synced": True,
        "last_checked_at": last,
        "gmail_disconnected": bool(state.get("gmail_disconnected_at")),
    }


def _quick_stats_payload(*, today_sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = now_ist()
    upcoming = get_upcoming_assignments(limit=200, now=now)
    due_week_end = now + timedelta(days=7)
    deadlines_due = sum(
        1
        for a in upcoming
        if (dt := item_effective_deadline_dt(a)) is not None and dt <= due_week_end
    )
    if today_sessions is None:
        section = get_preferred_section()
        today_sessions = sessions_for_day(today_ist(), section=section)
    placement = sum(1 for a in get_open_assignments(limit=200) if a.get("category") == "placement")
    return {
        "deadlines_due": deadlines_due,
        "classes_today": sum(1 for s in today_sessions if not s.get("cancelled")),
        "placement": placement,
    }


@router.get("/stats/quick")
async def quick_stats():
    return _quick_stats_payload()


def _activity_items(limit: int = 10) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    since = datetime.now(timezone.utc) - timedelta(hours=48)
    try:
        db = get_db()
        result = (
            db.table("assignments")
            .select(_ASSIGNMENT_WEB_COLUMNS)
            .gte("created_at", since.isoformat())
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        for row in result.data or []:
            items.append(
                {
                    "type": "assignment",
                    "title": row.get("task") or row.get("summary"),
                    "category": correct_category_from_text(
                        row.get("category"),
                        row.get("task"),
                        row.get("summary"),
                        row.get("subject"),
                        row.get("course_or_topic"),
                    ),
                    "created_at": row.get("created_at"),
                    "sender": row.get("sender"),
                }
            )
    except Exception as exc:
        logger.warning("activity feed failed: %s", exc)
    return items


@router.get("/dashboard")
async def dashboard():
    now = now_ist()
    section = get_preferred_section()
    settings = get_settings()
    upcoming = get_upcoming_assignments(limit=20, now=now)
    urgent = upcoming[0] if upcoming else None
    today_sessions = sessions_for_day(today_ist(), section=section)
    stats = _quick_stats_payload(today_sessions=today_sessions)
    db_health = check_db_health()
    return {
        "greeting_name": _greeting_name(),
        "student_name": settings.student_name,
        "section": section,
        "sync": _sync_meta(),
        "db_ok": bool(db_health.get("ok")),
        "stats": stats,
        "urgent_task": _serialize_task(urgent, now=now) if urgent else None,
        "today_classes": [_serialize_class(s) for s in today_sessions],
        "upcoming_deadlines": [_serialize_task(a, now=now) for a in upcoming[:8]],
        "activity": {"items": _activity_items(5)},
    }


@router.get("/activity/recent")
async def recent_activity(limit: int = Query(default=10, ge=1, le=50)):
    return {"items": _activity_items(limit)}


@router.get("/tasks")
async def list_tasks(
    status: Literal["all", "due-soon", "missed", "undated"] = "all",
    category: str | None = None,
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
):
    now = now_ist()
    cat = None if not category or category == "all" else category
    if status == "missed":
        raw = get_missed_assignments(category=cat, limit=limit, now=now)
    elif status == "undated":
        raw = get_undated_assignments(category=cat, limit=limit, now=now)
    elif status == "due-soon":
        raw = get_upcoming_assignments(category=cat, limit=limit, now=now)
    else:
        raw = get_open_assignments(category=cat, limit=limit)
        done = []
        try:
            db = get_db()
            done_result = (
                db.table("assignments")
                .select(_ASSIGNMENT_WEB_COLUMNS)
                .eq("status", "done")
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            done = done_result.data or []
        except Exception:
            pass
        raw = raw + done

    tasks = [_serialize_task(t, now=now) for t in raw]
    if q:
        needle = q.lower().strip()
        tasks = [
            t
            for t in tasks
            if needle in (t.get("task") or "").lower()
            or needle in (t.get("sender") or "").lower()
            or needle in (t.get("category") or "").lower()
        ]
    counts = {
        "all": len(get_open_assignments(limit=200)),
        "due-soon": len(get_upcoming_assignments(limit=200, now=now)),
        "missed": len(get_missed_assignments(limit=200, now=now)),
        "undated": len(get_undated_assignments(limit=200, now=now)),
    }
    return {"tasks": tasks, "counts": counts}


class TaskCreateBody(BaseModel):
    task: str
    deadline_text: str | None = None
    category: str = "other"
    course_or_topic: str | None = None


@router.post("/tasks")
async def create_task(body: TaskCreateBody):
    row = await asyncio.to_thread(
        create_manual_assignment,
        body.task,
        deadline_text=body.deadline_text,
        category=body.category,
        course_or_topic=body.course_or_topic,
    )
    if not row:
        raise HTTPException(status_code=400, detail="Could not create task")
    task = _serialize_task(row)
    await broadcast("task_updated", {"action": "created", "task": task})
    return task


class TaskUpdateBody(BaseModel):
    action: Literal["done", "reopen", "snooze", "dismiss", "edit"] | None = None
    snooze_hours: int | None = Field(default=2, ge=1, le=72)
    task: str | None = None
    deadline_text: str | None = None
    category: str | None = None


@router.put("/tasks/{assignment_id}")
async def update_task(assignment_id: str, body: TaskUpdateBody):
    action = body.action or "edit"
    row: dict[str, Any] | None = None
    if action == "done":
        row = await asyncio.to_thread(mark_assignment_done, assignment_id)
    elif action == "dismiss":
        row = await asyncio.to_thread(dismiss_assignment, assignment_id)
    elif action == "reopen":
        db = get_db()
        result = (
            db.table("assignments")
            .update({"status": "open"})
            .eq("id", assignment_id)
            .execute()
        )
        rows = result.data or []
        row = rows[0] if rows else None
    elif action == "snooze":
        remind_at = now_ist() + timedelta(hours=body.snooze_hours or 2)
        row = await asyncio.to_thread(schedule_assignment_reminder, assignment_id, remind_at)
    elif action == "edit":
        fields: dict[str, Any] = {}
        if body.task:
            fields["task"] = body.task
        if body.deadline_text is not None:
            fields["deadline_text"] = body.deadline_text
        if body.category:
            fields["category"] = body.category
        row = await asyncio.to_thread(update_assignment, assignment_id, fields)
        if not row:
            db = get_db()
            payload = {k: v for k, v in fields.items() if v is not None}
            if payload:
                result = db.table("assignments").update(payload).eq("id", assignment_id).execute()
                rows = result.data or []
                row = rows[0] if rows else get_assignment_by_id(assignment_id)
    else:
        raise HTTPException(status_code=400, detail="Unknown action")

    if not row:
        raise HTTPException(status_code=404, detail="Task not found or update failed")
    task = _serialize_task(row)
    await broadcast("task_updated", {"action": action, "task": task})
    return task


@router.delete("/tasks/{assignment_id}")
async def delete_task(assignment_id: str):
    row = await asyncio.to_thread(dismiss_assignment, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    await broadcast("task_updated", {"action": "dismissed", "id": assignment_id})
    return {"ok": True}


def _schedule_for_day(day: date, *, refresh: bool = False) -> dict[str, Any]:
    section = get_preferred_section()
    if refresh:
        try:
            refresh_schedule_from_gmail()
        except Exception as exc:
            logger.warning("schedule refresh: %s", exc)
    sessions = sessions_for_day(day, section=section)
    return {
        "date": day.isoformat(),
        "section": section,
        "sessions": [_serialize_class(s) for s in sessions],
        "cache_fresh": schedule_cache_is_fresh(),
    }


@router.get("/schedule/today")
async def schedule_today(refresh: bool = Query(default=False)):
    return _schedule_for_day(today_ist(), refresh=refresh)


@router.get("/schedule/week")
async def schedule_week(start: date | None = None):
    anchor = start or today_ist()
    week_start = anchor - timedelta(days=anchor.weekday())
    days: list[dict[str, Any]] = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        days.append(_schedule_for_day(day))
    return {"week_start": week_start.isoformat(), "days": days}


@router.get("/schedule/{day}")
async def schedule_day(day: date, refresh: bool = Query(default=False)):
    return _schedule_for_day(day, refresh=refresh)


@router.post("/schedule/refresh")
async def schedule_refresh():
    try:
        info = await asyncio.to_thread(refresh_schedule_from_gmail)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payload = _schedule_for_day(today_ist())
    payload["source"] = info
    await broadcast("sync_status", {"schedule_refreshed": True})
    return payload


class ChatMessageBody(BaseModel):
    message: str
    verified: bool = False


def _looks_like_compose(text: str) -> tuple[str | None, str | None]:
    intent = classify_message(text, convo=get_web_convo())
    if intent.get("intent") == INTENT_COMPOSE:
        recipient = intent.get("recipient") or intent.get("person")
        content = intent.get("content") or text
        return recipient, content
    m = re.search(
        r"\b(?:send|write|draft|compose|reply|email|mail)\b.+\bto\b\s+(.+?)(?:\s+(?:saying|that)\s+(.+))?$",
        text,
        re.I | re.S,
    )
    if m:
        return m.group(1).strip(), (m.group(2) or text).strip()
    return None, None


@router.get("/chat/history")
async def chat_history():
    return {"messages": chat_history_for_api()}


@router.post("/chat/message")
async def chat_message(body: ChatMessageBody):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")

    remember_web_turn(user_text=text)
    recipient_hint, compose_content = _looks_like_compose(text)
    draft_payload: dict[str, Any] | None = None

    if recipient_hint and compose_content:
        email = recipient_hint if "@" in recipient_hint else resolve_contact_email(recipient_hint)
        instructions = f"To: {email or recipient_hint}\n\n{compose_content}"
        draft = await asyncio.to_thread(compose_email, instructions)
        if draft and draft.get("subject") and draft.get("body"):
            draft_payload = {
                "to": email or recipient_hint,
                "to_display": recipient_hint,
                "subject": draft["subject"],
                "body": draft["body"],
            }
            set_web_compose({"draft": draft_payload, "instructions": instructions})
            reply = (
                "I've drafted an email following standard academic etiquette. "
                "Review the preview below and send when ready."
            )
            remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
            return {"reply": reply, "draft": draft_payload}

    items = get_assignments_for_qa(limit=40)
    reply = await asyncio.to_thread(
        answer_question,
        text,
        items,
        scan_gmail=True,
        verified=body.verified,
        convo=get_web_convo(),
    )
    remember_web_turn(bot_text=reply, intent="qa")
    return {"reply": reply, "draft": draft_payload}


class ComposeDraftBody(BaseModel):
    instructions: str


@router.post("/compose/draft")
async def compose_draft(body: ComposeDraftBody):
    draft = await asyncio.to_thread(compose_email, body.instructions)
    if not draft or "subject" not in draft or "body" not in draft:
        raise HTTPException(status_code=502, detail="Could not generate draft")
    set_web_compose({"draft": draft, "instructions": body.instructions})
    return draft


class ComposeSendBody(BaseModel):
    to: str
    subject: str
    body: str


@router.post("/compose/send")
async def compose_send(body: ComposeSendBody):
    to = body.to.strip()
    if not to:
        raise HTTPException(status_code=400, detail="Missing recipient")
    try:
        await asyncio.to_thread(send_email, to, body.subject, body.body)
    except Exception as exc:
        logger.exception("compose send failed: %s", exc)
        raise HTTPException(status_code=502, detail="Send failed") from exc
    set_web_compose(None)
    await broadcast("sync_status", {"email_sent": True, "to": to})
    return {"ok": True, "to": to}


@router.post("/chat/reset")
async def chat_reset():
    clear_web_convo()
    return {"ok": True}
