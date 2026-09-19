"""REST API for the Mailbot mobile web client."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ai.assistant import (
    _deterministic_deadline_answer,
    _deterministic_undated_answer,
    _extract_person_query,
    _is_mailbox_question,
    answer_question,
    infer_person_email,
    lookup_contact_email,
)
from ai.composer import compose_email
from ai.intent import (
    INTENT_COMPOSE,
    INTENT_CONTACT_LOOKUP,
    INTENT_DUE_LIST,
    INTENT_MAILBOX_QA,
    INTENT_MISSED_LIST,
    INTENT_SCHEDULE,
    INTENT_UNDATED_LIST,
    _is_specific_deadline_question,
    classify_message,
)
from config import get_settings
from gmail_api.client import send_email
from services.contacts import (
    extract_person_reference,
    looks_like_recipient_switch,
    resolve_contact_email,
    resolve_email_robust,
    upsert_contact_from_sender,
)
from services.db import (
    create_manual_assignment,
    dismiss_assignment,
    get_assignment_by_id,
    get_assignments_for_qa,
    get_db,
    _filter_assignments_by_deadline,
    get_assignments_due_between,
    get_assignments_due_on,
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
    is_schedule_challenge,
    next_sessions_after,
    refresh_schedule_from_gmail,
    resolve_schedule_day,
    schedule_cache_is_fresh,
    sessions_for_day,
)
from ai.deadline import correct_category_from_text
from services.timeutil import (
    IST,
    format_deadline_display,
    item_effective_deadline_dt,
    now_ist,
    parse_day_window,
    today_ist,
)
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


def _partition_open_assignments(
    category: str | None = None,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    ref = now or now_ist()
    pool = get_open_assignments(category=category, limit=limit)
    upcoming = _filter_assignments_by_deadline(pool, upcoming=True, now=ref)
    missed = _filter_assignments_by_deadline(pool, upcoming=False, now=ref)
    undated = [item for item in pool if item_effective_deadline_dt(item) is None]
    return {"all": pool, "upcoming": upcoming, "missed": missed, "undated": undated}


def _quick_stats_payload(*, today_sessions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = now_ist()
    buckets = _partition_open_assignments(now=now)
    due_week_end = now + timedelta(days=7)
    deadlines_due = sum(
        1
        for a in buckets["upcoming"]
        if (dt := item_effective_deadline_dt(a)) is not None and dt <= due_week_end
    )
    if today_sessions is None:
        section = get_preferred_section()
        today_sessions = sessions_for_day(today_ist(), section=section)
    placement = sum(1 for a in buckets["all"] if a.get("category") == "placement")
    return {
        "deadlines_due": deadlines_due,
        "classes_today": sum(1 for s in today_sessions if not s.get("cancelled")),
        "placement": placement,
    }


def _web_format_sessions(sessions: list[dict[str, Any]], day: date) -> str:
    label = day.strftime("%A, %d %b")
    if not sessions:
        return f"No classes found for {label}."
    lines = [f"Classes — {label}:", ""]
    for session in sessions:
        course = session.get("course_or_topic") or "Class"
        start = session.get("start_time") or "—"
        end = session.get("end_time")
        time_bit = f"{start}–{end}" if end else str(start)
        room = session.get("room") or "Room TBA"
        note = " (cancelled)" if session.get("cancelled") else ""
        lines.append(f"• {time_bit} {course} ({room}){note}")
    return "\n".join(lines)


def _web_local_list_reply(intent: str, text: str) -> str | None:
    """DB-only answers for due / classes / missed / undated — skip Gmail + LLM."""
    now = now_ist()
    if intent == INTENT_SCHEDULE:
        day = resolve_schedule_day(text)
        section = get_preferred_section()
        sessions = sessions_for_day(day, section=section)
        if sessions:
            reply = _web_format_sessions(sessions, day)
            if is_schedule_challenge(text):
                return f"That was the last day I had stored. Today is {day.strftime('%A, %d %b')}.\n\n{reply}"
            return reply
        next_day, next_sessions = next_sessions_after(day, section=section)
        if next_day:
            preface = f"No classes on {day.strftime('%A, %d %b')}. Next sessions:"
            if is_schedule_challenge(text):
                preface = (
                    f"Sorry — I reused an old day. Today is {day.strftime('%A, %d %b')} "
                    "and there are no classes. Next sessions:"
                )
            return f"{preface}\n\n{_web_format_sessions(next_sessions, next_day)}"
        return _web_format_sessions([], day)
    if intent == INTENT_DUE_LIST:
        window = parse_day_window(text)
        if window is not None:
            if window.single:
                items = get_assignments_due_on(window.start, IST)
            else:
                items = get_assignments_due_between(window.start, window.end, IST)
        else:
            items = get_upcoming_assignments(limit=40, now=now)
        return _deterministic_deadline_answer(text, items, now=now) or (
            "No upcoming deadlines with a set due date." if not items else None
        )
    if intent == INTENT_MISSED_LIST:
        items = get_missed_assignments(limit=20, now=now)
        if not items:
            return "Nothing missed — you're caught up on tracked deadlines."
        lines = [f"Missed deadlines ({len(items)}):", ""]
        for index, item in enumerate(items[:15], start=1):
            task = item.get("task") or item.get("summary") or "Untitled"
            due = item.get("deadline_text") or "—"
            lines.append(f"{index}. {task} — {due}")
        return "\n".join(lines)
    if intent == INTENT_UNDATED_LIST:
        items = get_undated_assignments(limit=40, now=now)
        return _deterministic_undated_answer(items)
    return None


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
    settings = get_settings()

    def _load_core() -> tuple[Any, ...]:
        section = get_preferred_section()
        buckets = _partition_open_assignments(now=now)
        sessions = sessions_for_day(today_ist(), section=section)
        activity = _activity_items(5)
        sync = _sync_meta()
        return section, buckets, sessions, activity, sync

    try:
        section, buckets, today_sessions, activity, sync = await asyncio.to_thread(_load_core)
        db_ok = True
    except Exception:
        logger.exception("dashboard load failed")
        section, buckets, today_sessions, activity, sync = "A", {
            "all": [],
            "upcoming": [],
            "missed": [],
            "undated": [],
        }, [], [], _sync_meta()
        db_ok = False

    upcoming = buckets["upcoming"]
    urgent = upcoming[0] if upcoming else None
    due_week_end = now + timedelta(days=7)
    stats = {
        "deadlines_due": sum(
            1
            for a in upcoming
            if (dt := item_effective_deadline_dt(a)) is not None and dt <= due_week_end
        ),
        "classes_today": sum(1 for s in today_sessions if not s.get("cancelled")),
        "placement": sum(1 for a in buckets["all"] if a.get("category") == "placement"),
    }
    return {
        "greeting_name": _greeting_name(),
        "student_name": settings.student_name,
        "section": section,
        "sync": sync,
        "db_ok": db_ok,
        "stats": stats,
        "urgent_task": _serialize_task(urgent, now=now) if urgent else None,
        "today_classes": [_serialize_class(s) for s in today_sessions],
        "upcoming_deadlines": [_serialize_task(a, now=now) for a in upcoming[:8]],
        "activity": {"items": activity},
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
    buckets = _partition_open_assignments(category=cat, now=now)
    if status == "missed":
        raw = buckets["missed"][:limit]
    elif status == "undated":
        raw = buckets["undated"][:limit]
    elif status == "due-soon":
        raw = buckets["upcoming"][:limit]
    else:
        raw = buckets["all"][:limit]
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
    count_source = buckets if cat is None else _partition_open_assignments(now=now)
    counts = {
        "all": len(count_source["all"]),
        "due-soon": len(count_source["upcoming"]),
        "missed": len(count_source["missed"]),
        "undated": len(count_source["undated"]),
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


def _content_is_command_only(text: str, recipient: str | None) -> bool:
    """True when the 'content' is just the send command, not what to write."""
    q = (text or "").strip()
    if not q:
        return True
    if re.search(r"\b(?:saying|that|about|regarding)\b", q, flags=re.I) and len(q.split()) >= 8:
        return False
    stripped = q.lower()
    if recipient:
        stripped = stripped.replace(str(recipient).lower(), " ")
    stripped = re.sub(
        r"\b(?:please|kindly|can you|could you|i want to|i wanna|need to|send|write|draft|compose|reply|a|an|the|mail|email|to|for)\b",
        " ",
        stripped,
        flags=re.I,
    )
    leftover = re.sub(r"\s+", " ", stripped).strip(" .,?!")
    return len(leftover) < 4


async def _web_make_draft(email: str, display: str, instructions: str) -> dict[str, Any] | None:
    prompt = f"To: {email}\nRecipient name: {display}\n\n{instructions}"
    draft = await asyncio.to_thread(compose_email, prompt)
    if not draft or not draft.get("subject") or not draft.get("body"):
        return None
    payload = {
        "to": email,
        "to_display": display or email,
        "subject": draft["subject"],
        "body": draft["body"],
    }
    set_web_compose(
        {
            "step": "confirm",
            "recipient": email,
            "recipient_name": display,
            "instructions": instructions,
            "draft": payload,
        }
    )
    return payload


async def _web_begin_compose(text: str, classified: dict[str, Any]) -> dict[str, Any]:
    convo = get_web_convo()
    recipient = classified.get("recipient") or classified.get("person")
    content = classified.get("content")
    if content and _content_is_command_only(content, recipient):
        content = None
    if content and _content_is_command_only(text, recipient) and content.strip().lower() == text.strip().lower():
        content = None

    email = None
    note = None
    if recipient:
        email, _matches, note = await asyncio.to_thread(resolve_email_robust, recipient, convo=convo)

    if email and content:
        draft = await _web_make_draft(email, recipient, content)
        if draft:
            reply = f"Draft ready for {draft['to_display']} ({email}). Review and send when ready."
            remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=recipient, person_email=email)
            return {"reply": reply, "draft": draft}
        reply = "Couldn't draft that just now. Try again in a minute."
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
        return {"reply": reply, "draft": None}

    if email and not content:
        set_web_compose({"step": "need_content", "recipient": email, "recipient_name": recipient})
        reply = f"Got it — I'll send to {recipient} ({email}). What should the email say?"
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=recipient, person_email=email)
        return {"reply": reply, "draft": None}

    if content and not email:
        set_web_compose({"step": "need_recipient", "instructions": content, "pending_recipient": recipient})
        reply = note or "Who should I send it to? Give a name or email address."
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=recipient)
        return {"reply": reply, "draft": None}

    if recipient and not email:
        set_web_compose({"step": "need_recipient", "instructions": content, "pending_recipient": recipient})
        reply = note or (
            f"I don't have an email for {recipient} yet. "
            "Paste their address, or say the name again after I've seen mail from them."
        )
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=recipient)
        return {"reply": reply, "draft": None}

    set_web_compose({"step": "need_recipient", "instructions": content})
    reply = "Who should I send it to? Name or email address."
    remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
    return {"reply": reply, "draft": None}


async def _web_continue_compose(text: str, compose: dict[str, Any]) -> dict[str, Any]:
    convo = get_web_convo()
    step = compose.get("step")
    lower = text.strip().lower()
    if lower in {"cancel", "stop", "discard", "abort"}:
        set_web_compose(None)
        reply = "Cancelled. Draft discarded."
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
        return {"reply": reply, "draft": None}

    if step == "need_recipient":
        mention = extract_person_reference(text, convo) or text.strip()
        email, _matches, note = await asyncio.to_thread(resolve_email_robust, mention, convo=convo)
        if not email:
            reply = note or "I still don't have that email. Paste the address?"
            remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention)
            return {"reply": reply, "draft": None}
        compose["recipient"] = email
        compose["recipient_name"] = mention
        compose.pop("pending_recipient", None)
        if compose.get("instructions"):
            draft = await _web_make_draft(email, mention, compose["instructions"])
            if draft:
                reply = f"Draft ready for {mention} ({email}). Review and send when ready."
                remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention, person_email=email)
                return {"reply": reply, "draft": draft}
        compose["step"] = "need_content"
        set_web_compose(compose)
        reply = f"Got it — I'll send to {mention} ({email}). What should the email say?"
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention, person_email=email)
        return {"reply": reply, "draft": None}

    if step == "need_content":
        if looks_like_recipient_switch(text):
            mention = extract_person_reference(text, convo)
            if mention:
                email, _matches, note = await asyncio.to_thread(resolve_email_robust, mention, convo=convo)
                if email:
                    compose["recipient"] = email
                    compose["recipient_name"] = mention
                    extra = classify_message(text, convo).get("content")
                    if extra:
                        compose["instructions"] = extra
                    if compose.get("instructions"):
                        draft = await _web_make_draft(email, mention, compose["instructions"])
                        if draft:
                            reply = f"Updated the recipient to {mention} ({email}). Draft is ready."
                            remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention, person_email=email)
                            return {"reply": reply, "draft": draft}
                    compose["step"] = "need_content"
                    set_web_compose(compose)
                    reply = f"Got it — I'll send to {mention} ({email}). What should the email say?"
                    remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention, person_email=email)
                    return {"reply": reply, "draft": None}
                reply = note or "I couldn't resolve that recipient. Paste their email?"
                remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention)
                return {"reply": reply, "draft": None}
        compose["instructions"] = text.strip()
        email = compose.get("recipient")
        display = compose.get("recipient_name") or email
        if email:
            draft = await _web_make_draft(email, display, text.strip())
            if draft:
                reply = f"Draft ready for {display}. Review and send when ready."
                remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=display, person_email=email)
                return {"reply": reply, "draft": draft}
        compose["step"] = "need_recipient"
        set_web_compose(compose)
        reply = "Who should I send it to? Name or email address."
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
        return {"reply": reply, "draft": None}

    if step == "confirm" and re.search(
        r"\b(?:instead|not (?:her|him|them)|change (?:the )?(?:to|recipient)|send (?:it |this |that )?to)\b",
        lower,
    ):
        mention = extract_person_reference(text, convo)
        if mention:
            email, _matches, note = await asyncio.to_thread(resolve_email_robust, mention, convo=convo)
            if email:
                instructions = compose.get("instructions") or "Follow up politely."
                draft = await _web_make_draft(email, mention, instructions)
                if draft:
                    reply = f"Updated the recipient to {mention} ({email}). Draft is ready."
                    remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention, person_email=email)
                    return {"reply": reply, "draft": draft}
            reply = note or "I couldn't resolve that recipient. Paste their email?"
            remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE, person=mention)
            return {"reply": reply, "draft": None}

    if step == "confirm" and any(w in lower for w in ("edit", "change", "revise", "redo")):
        compose["step"] = "need_content"
        set_web_compose(compose)
        reply = "What should I change? Describe the new content."
        remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
        return {"reply": reply, "draft": None}

    draft = compose.get("draft")
    reply = "Draft is ready below. Send it, or say edit / cancel."
    remember_web_turn(bot_text=reply, intent=INTENT_COMPOSE)
    return {"reply": reply, "draft": draft}


@router.get("/chat/history")
async def chat_history():
    return {"messages": chat_history_for_api()}


@router.post("/chat/message")
async def chat_message(body: ChatMessageBody):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")

    remember_web_turn(user_text=text)
    compose = get_web_compose()
    if compose and compose.get("step"):
        return await _web_continue_compose(text, compose)

    classified = classify_message(text, convo=get_web_convo())
    convo = get_web_convo()
    if re.search(r"\b(?:claude|groq|nemotron|provider|ai provider)\b", text, re.I) and re.search(
        r"\b(?:why|reach|use|try|not working|down|fail|instead)\b", text, re.I
    ):
        from ai.client import get_llm_status

        status = get_llm_status()
        configured = ", ".join(status.get("providers_configured") or []) or "none configured"
        last_ok = status.get("last_ok_provider") or "none recently"
        cooled = status.get("cooldowns_seconds") or {}
        reply = f"I try providers in this order: {configured}. Last one that worked: {last_ok}."
        if cooled:
            bits = [f"{name} ({seconds}s left)" for name, seconds in cooled.items()]
            reply += " On cooldown: " + ", ".join(bits) + "."
        else:
            reply += " None are on cooldown — if a reply just failed I will try Claude next."
        remember_web_turn(bot_text=reply, intent="smalltalk")
        return {"reply": reply, "draft": None}

    bare = re.fullmatch(r"\s*([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\s*", text)
    if bare:
        addr = bare.group(1).lower()
        person = convo.get("last_person")
        if person:
            await asyncio.to_thread(upsert_contact_from_sender, f"{person} <{addr}>")
        last_intent = convo.get("last_intent")
        if last_intent in {INTENT_COMPOSE, INTENT_CONTACT_LOOKUP} or get_web_compose():
            classified = {"intent": INTENT_COMPOSE, "recipient": addr, "content": None}
            return await _web_begin_compose(text, classified)
        reply = await asyncio.to_thread(
            answer_question,
            f"mails from {addr}",
            get_assignments_for_qa(limit=40),
            scan_gmail=True,
            convo=convo,
            person_hint=person or addr,
        )
        remember_web_turn(bot_text=reply, intent=INTENT_MAILBOX_QA, person=person, person_email=addr)
        return {"reply": reply, "draft": None}

    if classified.get("intent") == INTENT_COMPOSE:
        return await _web_begin_compose(text, classified)
    if classified.get("intent") == INTENT_CONTACT_LOOKUP:
        convo = get_web_convo()
        person = extract_person_reference(classified.get("person"), convo) or classified.get("person")
        reply = await asyncio.to_thread(lookup_contact_email, person, convo=convo)
        remember_web_turn(bot_text=reply, intent=INTENT_CONTACT_LOOKUP, person=person)
        return {"reply": reply, "draft": None}

    intent = classified.get("intent")
    if (
        intent in (INTENT_DUE_LIST, INTENT_SCHEDULE, INTENT_MISSED_LIST, INTENT_UNDATED_LIST)
        and not _is_specific_deadline_question(text)
    ):
        reply = await asyncio.to_thread(_web_local_list_reply, intent, text)
        if reply:
            remember_web_turn(bot_text=reply, intent=intent)
            return {"reply": reply, "draft": None}

    items = get_assignments_for_qa(limit=40)
    convo = get_web_convo()
    person = (
        classified.get("person")
        or _extract_person_query(text)
        or extract_person_reference(text, convo)
    )
    scan_gmail = bool(
        body.verified
        or intent == INTENT_MAILBOX_QA
        or _is_mailbox_question(text)
    )
    reply = await asyncio.to_thread(
        answer_question,
        text,
        items,
        scan_gmail=scan_gmail,
        verified=body.verified,
        convo=convo,
        person_hint=person,
    )
    person_email = None
    if person:
        person_email = await asyncio.to_thread(
            infer_person_email, person, convo=convo, live=False
        )
    remember_web_turn(bot_text=reply, intent=intent or "qa", person=person, person_email=person_email)
    return {"reply": reply, "draft": None}


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
    if "@" not in to:
        email, _matches, note = await asyncio.to_thread(
            resolve_email_robust, to, convo=get_web_convo()
        )
        if not email:
            raise HTTPException(status_code=400, detail=note or "Could not resolve recipient email")
        to = email
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
