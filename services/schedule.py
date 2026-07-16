"""Parse and store college class timetables."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from ai.client import call_json
from ai.prompts import SCHEDULE_PROMPT
from services.db import delete_class_sessions_for_week, get_classes_on, insert_class_sessions
from services.timeutil import IST, today_ist

logger = logging.getLogger(__name__)


def looks_like_schedule(subject: str, body: str, attachment_text: str, result: dict[str, Any]) -> bool:
    if result.get("is_schedule"):
        return True
    blob = f"{subject}\n{body}\n{attachment_text}".lower()
    keywords = (
        "timetable",
        "time table",
        "class schedule",
        "weekly schedule",
        "schedule for",
        "classroom",
        "room no",
        "lecture hall",
    )
    return any(k in blob for k in keywords)


def parse_schedule_sessions(
    *,
    subject: str,
    body: str,
    attachment_text: str,
    received_at: str | None,
) -> list[dict[str, Any]]:
    ref = today_ist()
    if received_at:
        try:
            dt = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            ref = dt.astimezone(IST).date()
        except ValueError:
            pass

    system = SCHEDULE_PROMPT.format(today=ref.isoformat())
    user = (
        f"Subject: {subject}\n\n"
        f"Email body:\n{body[:5000]}\n\n"
        f"Attachment text:\n{(attachment_text or '')[:6000]}"
    )
    parsed = call_json(system, user)
    if not parsed or not isinstance(parsed.get("sessions"), list):
        return []

    week_start = parsed.get("week_start")
    sessions: list[dict[str, Any]] = []
    for raw in parsed["sessions"]:
        if not isinstance(raw, dict):
            continue
        session_date = raw.get("session_date")
        course = raw.get("course_or_topic")
        start = raw.get("start_time")
        if not session_date or not course or not start:
            continue
        sessions.append(
            {
                "course_or_topic": str(course)[:200],
                "session_date": str(session_date)[:10],
                "start_time": str(start)[:5],
                "end_time": (str(raw["end_time"])[:5] if raw.get("end_time") else None),
                "room": (str(raw["room"])[:120] if raw.get("room") else None),
                "faculty": (str(raw["faculty"])[:120] if raw.get("faculty") else None),
                "notes": (str(raw["notes"])[:300] if raw.get("notes") else None),
                "week_start": (str(week_start)[:10] if week_start else None),
            }
        )
    return sessions


def store_schedule_sessions(
    sessions: list[dict[str, Any]],
    *,
    gmail_message_id: str,
) -> list[dict[str, Any]]:
    if not sessions:
        return []
    week_starts = {s.get("week_start") for s in sessions if s.get("week_start")}
    for week in week_starts:
        try:
            delete_class_sessions_for_week(week)
        except Exception as exc:
            logger.warning("Could not clear week %s sessions: %s", week, exc)

    payload = [{**s, "gmail_message_id": gmail_message_id} for s in sessions]
    return insert_class_sessions(payload)


def format_schedule_share(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "No class sessions could be extracted from this timetable."

    by_day: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        by_day.setdefault(s["session_date"], []).append(s)

    lines = ["<b>📅 Class schedule received</b>", ""]
    for day in sorted(by_day.keys()):
        try:
            label = date.fromisoformat(day).strftime("%a, %d %b")
        except ValueError:
            label = day
        lines.append(f"<b>{label}</b>")
        day_sessions = sorted(by_day[day], key=lambda x: x.get("start_time") or "")
        for s in day_sessions:
            course = s.get("course_or_topic") or "Class"
            start = s.get("start_time") or "—"
            end = s.get("end_time")
            time_bit = f"{start}" + (f"–{end}" if end else "")
            room = s.get("room") or "Room TBA"
            lines.append(f"• {time_bit} — {course} ({room})")
        lines.append("")
    return "\n".join(lines).strip()


def format_today_classes(day: date | None = None) -> str:
    day = day or today_ist()
    sessions = get_classes_on(day.isoformat())
    if not sessions:
        return ""
    label = day.strftime("%a, %d %b %Y")
    lines = [f"<b>📚 Today's classes — {label}</b>", ""]
    for s in sorted(sessions, key=lambda x: x.get("start_time") or ""):
        course = s.get("course_or_topic") or "Class"
        start = s.get("start_time") or "—"
        end = s.get("end_time")
        time_bit = f"{start}" + (f"–{end}" if end else "")
        room = s.get("room") or "Room TBA"
        faculty = s.get("faculty")
        line = f"• <b>{time_bit}</b> — {course}\n   Room: {room}"
        if faculty:
            line += f"\n   Faculty: {faculty}"
        lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def next_week_monday(from_day: date | None = None) -> date:
    d = from_day or today_ist()
    # Monday = 0
    days_ahead = (7 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)
