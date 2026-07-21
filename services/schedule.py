"""Parse and store college class timetables from Gmail (body + PDF/Excel)."""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from ai.client import call_json
from ai.prompts import SCHEDULE_PROMPT
from gmail_api.client import get_message_details, search_message_ids
from services.db import (
    delete_class_sessions_for_message,
    get_class_sessions_for_message,
    get_classes_between,
    get_preferred_section,
    insert_class_sessions,
)
from services.timeutil import IST, today_ist

logger = logging.getLogger(__name__)


_SECTION_RE = re.compile(
    r"(?:sec(?:tion)?\.?\s*[-:]?\s*|sec\s*)([A-Za-z])\b"
    r"|\b([A-Za-z])\s*section\b"
    r"|\(\s*([A-Za-z])\s*\)",
    re.IGNORECASE,
)


def _session_section(course_or_topic: str, *extra_fields: str | None) -> str | None:
    """Pull section letter from course title / notes / room (e.g. Sec. C, Section A)."""
    blob = " ".join(str(x) for x in (course_or_topic, *extra_fields) if x)
    if not blob.strip():
        return None
    m = _SECTION_RE.search(blob)
    if not m:
        return None
    letter = next((g for g in m.groups() if g), None)
    return letter.upper() if letter else None


def _session_section_from_row(session: dict) -> str | None:
    return _session_section(
        session.get("course_or_topic") or "",
        session.get("notes"),
        session.get("room"),
        session.get("faculty"),
    )


def filter_by_section(sessions: list[dict], section: str | None) -> list[dict]:
    """Keep sessions for the preferred section.

    Unlabeled rows (no Sec. X in the text) are kept — many timetables only
    label the sheet header, not every cell. If every labeled row is a different
    section and the filter would empty the list, fall back to all sessions.
    """
    if not section:
        return sessions
    target = section.strip().upper()[:1]
    matched: list[dict] = []
    unlabeled: list[dict] = []
    for s in sessions:
        sec = _session_section_from_row(s)
        if sec is None:
            unlabeled.append(s)
        elif sec == target:
            matched.append(s)
    if matched:
        return matched + unlabeled
    if unlabeled and not any(_session_section_from_row(s) for s in sessions):
        # No rows carry a section tag at all — don't hide the timetable
        return sessions
    return matched  # may be empty when only other sections are labeled


def _session_date_value(session: dict[str, Any]) -> date | None:
    raw = session.get("session_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def project_sessions_to_day(sessions: list[dict[str, Any]], target_day: date) -> list[dict[str, Any]]:
    """Map a weekly timetable template onto a calendar day by weekday."""
    if not sessions:
        return []
    target_weekday = target_day.weekday()
    projected: list[dict[str, Any]] = []
    for session in sessions:
        session_day = _session_date_value(session)
        if session_day is None or session_day.weekday() != target_weekday:
            continue
        projected.append({**session, "session_date": target_day.isoformat()})
    return sorted(projected, key=lambda x: x.get("start_time") or "")


def project_sessions_to_week(
    sessions: list[dict[str, Any]],
    week_start: date,
    *,
    days: int = 7,
) -> list[dict[str, Any]]:
    """Project template sessions onto each day from week_start for `days` days."""
    if not sessions or days <= 0:
        return []
    projected: list[dict[str, Any]] = []
    for offset in range(days):
        day = week_start + timedelta(days=offset)
        projected.extend(project_sessions_to_day(sessions, day))
    return sorted(
        projected,
        key=lambda x: (x.get("session_date") or "", x.get("start_time") or ""),
    )


def load_template_sessions(info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Load the weekly timetable template from cache, DB, or a refresh payload."""
    if info and info.get("sessions"):
        return list(info["sessions"])
    for message_id in find_latest_schedule_message_ids(max_candidates=3):
        try:
            stored = get_class_sessions_for_message(message_id)
        except Exception as exc:
            logger.warning("DB lookup for schedule message %s failed: %s", message_id, exc)
            stored = []
        if stored:
            return stored
    try:
        anchor = today_ist()
        week_start = anchor - timedelta(days=anchor.weekday())
        week_end = week_start + timedelta(days=6)
        return get_classes_between(week_start.isoformat(), week_end.isoformat())
    except Exception as exc:
        logger.warning("Fallback schedule template load failed: %s", exc)
        return []


def sessions_for_day(
    target_day: date,
    *,
    section: str | None = None,
    template: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return projected classes for a calendar day, optionally filtered by section."""
    raw = template if template is not None else load_template_sessions()
    return filter_by_section(project_sessions_to_day(raw, target_day), section)


# In-process cache: avoid re-calling the LLM for the same timetable mail
_parse_cache: dict[str, dict[str, Any]] = {}

# Newest-first Gmail searches for timetable mails (incl. PDF attachments)
_SCHEDULE_QUERIES = (
    'subject:(timetable OR "time table" OR "class schedule" OR "weekly schedule" OR "class timings") newer_than:90d',
    '("timetable" OR "time table" OR "class schedule" OR "weekly schedule") has:attachment newer_than:90d',
    '(filename:timetable OR filename:schedule OR filename:"time table") newer_than:90d',
    'subject:(MBA OR PGDM OR "batch") (timetable OR schedule OR classroom) newer_than:60d',
)


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
        "class timings",
        "session timings",
    )
    return any(k in blob for k in keywords)


def _attachment_names_look_like_schedule(attachments: list[dict[str, Any]]) -> bool:
    for att in attachments or []:
        name = (att.get("filename") or "").lower()
        if any(k in name for k in ("timetable", "time table", "schedule", "class", "roster")):
            return True
        mime = (att.get("mime_type") or "").lower()
        if "pdf" in mime or name.endswith((".pdf", ".xlsx", ".xls", ".docx")):
            # Prefer docs attached to schedule-ish threads; final filter is parse success
            if any(k in name for k in ("time", "tt", "week", "batch", "mba", "pgdm")):
                return True
    return False


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
        f"Attachment text (PDF/Excel/Word — prefer this for rooms/times):\n"
        f"{(attachment_text or '')[:10000]}"
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
    try:
        delete_class_sessions_for_message(gmail_message_id)
    except Exception as exc:
        logger.warning("Could not clear sessions for message %s: %s", gmail_message_id, exc)

    payload = [{**s, "gmail_message_id": gmail_message_id} for s in sessions]
    try:
        return insert_class_sessions(payload)
    except Exception as exc:
        logger.warning("Could not store class sessions (table missing?): %s", exc)
        return sessions


def find_latest_schedule_message_ids(*, max_candidates: int = 8) -> list[str]:
    """Return newest-ish candidate message ids for timetable emails."""
    seen: list[str] = []
    for query in _SCHEDULE_QUERIES:
        try:
            ids = search_message_ids(query, max_results=6)
        except Exception as exc:
            logger.warning("Schedule Gmail search failed (%s): %s", query, exc)
            continue
        for mid in ids:
            if mid not in seen:
                seen.append(mid)
            if len(seen) >= max_candidates:
                return seen
    return seen


def _cached_or_stored_sessions(message_id: str) -> dict[str, Any] | None:
    """Reuse prior parse for the same Gmail message (no LLM)."""
    cached = _parse_cache.get(message_id)
    if cached and cached.get("sessions"):
        return cached
    try:
        stored = get_class_sessions_for_message(message_id)
    except Exception as exc:
        logger.warning("DB lookup for schedule message %s failed: %s", message_id, exc)
        stored = []
    if stored:
        info = {
            "ok": True,
            "message_id": message_id,
            "subject": cached.get("subject") if cached else None,
            "received_at": cached.get("received_at") if cached else None,
            "attachments": (cached or {}).get("attachments") or [],
            "had_attachment_text": True,
            "sessions": stored,
            "from_cache": True,
        }
        _parse_cache[message_id] = info
        return info
    return None


def refresh_schedule_from_gmail() -> dict[str, Any]:
    """
    Find the latest timetable mail, read body + PDF/Excel, parse sessions, store them.
    Returns metadata + sessions (even if DB insert fails).
    Skips LLM when this message was already parsed successfully.
    """
    candidates = find_latest_schedule_message_ids()
    if not candidates:
        return {"ok": False, "reason": "no_mail", "sessions": []}

    last_error = ""
    for message_id in candidates:
        reused = _cached_or_stored_sessions(message_id)
        if reused:
            logger.info("Reusing cached schedule parse for message %s", message_id)
            return reused

        try:
            details = get_message_details(message_id, load_attachments=True)
        except Exception as exc:
            logger.warning("Failed loading schedule candidate %s: %s", message_id, exc)
            last_error = str(exc)
            continue

        subject = details.get("subject") or ""
        body = details.get("body") or ""
        attachment_text = details.get("attachment_text") or ""
        attachments = details.get("attachments") or []
        att_names = [a.get("filename") for a in attachments if a.get("filename")]

        is_schedule = looks_like_schedule(subject, body, attachment_text, {"is_schedule": False})
        if not is_schedule and not _attachment_names_look_like_schedule(attachments):
            # Still try the newest candidate if it has a PDF and any schedule keyword in subject
            if not (attachments and any(k in subject.lower() for k in ("class", "schedule", "time", "tt"))):
                continue

        if not attachment_text and attachments:
            last_error = "pdf_unreadable"
            logger.warning(
                "Schedule mail %s has attachments %s but no extractable text",
                message_id,
                att_names,
            )

        sessions = parse_schedule_sessions(
            subject=subject,
            body=body,
            attachment_text=attachment_text,
            received_at=details.get("received_at_iso") or details.get("received_at"),
        )
        if not sessions:
            last_error = last_error or "parse_empty"
            continue

        stored = store_schedule_sessions(sessions, gmail_message_id=message_id)
        info = {
            "ok": True,
            "message_id": message_id,
            "subject": subject,
            "received_at": details.get("received_at"),
            "attachments": att_names,
            "had_attachment_text": bool(attachment_text.strip()),
            "sessions": stored or sessions,
            "from_cache": False,
        }
        _parse_cache[message_id] = info
        return info

    reason = last_error or "no_parse"
    return {"ok": False, "reason": reason, "sessions": []}


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
        lines.append(f"<b>{html.escape(str(label))}</b>")
        day_sessions = sorted(by_day[day], key=lambda x: x.get("start_time") or "")
        for s in day_sessions:
            course = html.escape(str(s.get("course_or_topic") or "Class"))
            start = html.escape(str(s.get("start_time") or "—"))
            end = s.get("end_time")
            time_bit = start + (f"–{html.escape(str(end))}" if end else "")
            room = html.escape(str(s.get("room") or "Room TBA"))
            lines.append(f"• {time_bit} — {course} ({room})")
        lines.append("")
    return "\n".join(lines).strip()


def _format_session_block(sessions: list[dict[str, Any]], title: str) -> str:
    if not sessions:
        return ""
    lines = [title, ""]
    for s in sorted(
        sessions,
        key=lambda x: (x.get("session_date") or "", x.get("start_time") or ""),
    ):
        course = html.escape(str(s.get("course_or_topic") or "Class"))
        start = html.escape(str(s.get("start_time") or "—"))
        end = s.get("end_time")
        time_bit = start + (f"–{html.escape(str(end))}" if end else "")
        room = html.escape(str(s.get("room") or "Room TBA"))
        faculty = s.get("faculty")
        day_label = ""
        try:
            day_label = html.escape(
                date.fromisoformat(str(s.get("session_date"))).strftime("%a %d %b")
            ) + " · "
        except (TypeError, ValueError):
            pass
        line = f"• <b>{day_label}{time_bit}</b> — {course}\n   Room: {room}"
        if faculty:
            line += f"\n   Faculty: {html.escape(str(faculty))}"
        lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


def format_today_classes(
    day: date | None = None,
    *,
    section: str | None = None,
    template: list[dict[str, Any]] | None = None,
) -> str:
    day = day or today_ist()
    sessions = sessions_for_day(day, section=section, template=template)
    if not sessions:
        return ""
    label = day.strftime("%a, %d %b %Y")
    title = "Today's classes" if day == today_ist() else f"Classes — {label}"
    return _format_session_block(sessions, f"<b>📚 {title}</b>")


def format_upcoming_classes(
    day: date | None = None,
    *,
    days: int = 7,
    section: str | None = None,
    template: list[dict[str, Any]] | None = None,
) -> str:
    day = day or today_ist()
    end = day + timedelta(days=days)
    try:
        raw = template if template is not None else load_template_sessions()
        projected = project_sessions_to_week(raw, day, days=days + 1)
        sessions = filter_by_section(projected, section)
    except Exception as exc:
        logger.warning("Upcoming classes projection failed: %s", exc)
        return ""
    if not sessions:
        return ""
    label = f"{day.strftime('%d %b')} – {end.strftime('%d %b')}"
    return _format_session_block(sessions, f"<b>📚 Classes ({label})</b>")


def _source_footer(info: dict[str, Any]) -> str:
    parts: list[str] = []
    subject = info.get("subject")
    if subject:
        parts.append(f"Mail: {html.escape(str(subject)[:120])}")
    when = info.get("received_at")
    if when:
        parts.append(html.escape(str(when)))
    atts = info.get("attachments") or []
    if atts:
        names = ", ".join(html.escape(str(a)) for a in atts[:3] if a)
        parts.append(f"File: {names}")
    if not parts:
        return ""
    return "\n\n<i>" + " · ".join(parts) + "</i>"


def classes_command_text(*, refresh: bool = True, section: str | None = None) -> str:
    """
    Power /classes: loads the latest timetable mail (PDF/body), stores sessions,
    then returns today's list (or upcoming if nothing today).
    """
    day = today_ist()
    section = section or get_preferred_section()
    info: dict[str, Any] = {}
    if refresh:
        try:
            info = refresh_schedule_from_gmail()
        except Exception as exc:
            logger.exception("Schedule refresh failed: %s", exc)
            info = {"ok": False, "reason": str(exc), "sessions": []}

    template = load_template_sessions(info if info.get("sessions") else None)

    today_text = ""
    try:
        today_text = format_today_classes(day, section=section, template=template)
    except Exception as exc:
        logger.warning("format_today_classes failed: %s", exc)

    if today_text:
        return today_text + _source_footer(info)

    if template:
        upcoming_text = format_upcoming_classes(day, days=7, section=section, template=template)
        if upcoming_text:
            return upcoming_text + _source_footer(info)

        label = day.strftime("%a, %d %b %Y")
        return (
            f"No classes scheduled for {label} in your section"
            f"{f' (Sec. {section})' if section else ''}.\n"
            "Your timetable is loaded — try asking about another day, or run /section."
        ) + _source_footer(info)

    reason = info.get("reason")
    if reason == "no_mail":
        return (
            "Couldn't find a recent class timetable email in Gmail.\n"
            "Forward the latest schedule mail (with PDF) to this mailbox, then try /classes again."
        )
    if reason == "pdf_unreadable":
        return (
            "Found a timetable mail with a PDF, but couldn't read text from it "
            "(it may be a scanned image).\n"
            "If you can, send an Excel/Word version or a text PDF."
        )
    if reason == "parse_empty":
        return (
            "Found timetable-looking mail, but couldn't extract class times/rooms yet.\n"
            "Try again in a minute, or paste the schedule text here."
        )
    return (
        "No classes stored yet.\n"
        "I'll look at the latest timetable mail + PDF when you run /classes again."
    )


def next_week_monday(from_day: date | None = None) -> date:
    d = from_day or today_ist()
    # Monday = 0
    days_ahead = (7 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)
