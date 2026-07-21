"""Parse and store college class timetables from Gmail (body + PDF/Excel)."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# #region agent log
_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / "debug-d53dec.log"


def _agent_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    try:
        payload = {
            "sessionId": "d53dec",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


# #endregion

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


def _template_date_set(sessions: list[dict[str, Any]]) -> set[date]:
    dates: set[date] = set()
    for session in sessions:
        day = _session_date_value(session)
        if day is not None:
            dates.add(day)
    return dates


def project_sessions_to_day(sessions: list[dict[str, Any]], target_day: date) -> list[dict[str, Any]]:
    """Resolve classes for a calendar day.

    Prefer exact stored dates. Only remap by weekday when the template is a true
    multi-day weekly pattern — never invent next-week Mondays from a one-day dump.
    """
    if not sessions:
        return []

    exact = [
        {**s, "session_date": target_day.isoformat()}
        for s in sessions
        if _session_date_value(s) == target_day
    ]
    if exact:
        # #region agent log
        _agent_log(
            "B",
            "schedule.py:project_sessions_to_day",
            "exact date match",
            {
                "target": target_day.isoformat(),
                "exact_count": len(exact),
                "mode": "exact",
            },
        )
        # #endregion
        return sorted(exact, key=lambda x: x.get("start_time") or "")

    stored_dates = _template_date_set(sessions)
    stored_weekdays = {d.weekday() for d in stored_dates}
    # Single-day (or single-weekday) dumps are dated snapshots — do not project
    if len(stored_weekdays) <= 1:
        # #region agent log
        _agent_log(
            "A",
            "schedule.py:project_sessions_to_day",
            "skip weekday projection for single-day template",
            {
                "target": target_day.isoformat(),
                "stored_dates": sorted(d.isoformat() for d in stored_dates),
                "mode": "no_project",
                "projected_count": 0,
            },
        )
        # #endregion
        return []

    target_weekday = target_day.weekday()
    projected: list[dict[str, Any]] = []
    for session in sessions:
        session_day = _session_date_value(session)
        if session_day is None or session_day.weekday() != target_weekday:
            continue
        projected.append({**session, "session_date": target_day.isoformat()})
    # #region agent log
    _agent_log(
        "A",
        "schedule.py:project_sessions_to_day",
        "weekday projection",
        {
            "target": target_day.isoformat(),
            "weekday": target_weekday,
            "projected_count": len(projected),
            "template_size": len(sessions),
            "template_dates": sorted(d.isoformat() for d in stored_dates)[:20],
            "mode": "weekday",
        },
    )
    # #endregion
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


_DATE_MARK_RE = re.compile(r"(?P<d1>\d{2})\.(?P<m1>\d{2})\.(?P<y1>\d{2,4})")
_DAY_LABEL_RE = re.compile(r"\b(?P<label>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.IGNORECASE)


def _parse_day_marker_date(d1: str, m1: str, y1: str) -> date | None:
    try:
        year = int(y1)
        if year < 100:
            year += 2000
        return date(year, int(m1), int(d1))
    except ValueError:
        return None


def _split_timetable_day_chunks(attachment_text: str) -> list[tuple[date, str]]:
    """Split messy PDF timetable text into (calendar_date, chunk) pairs."""
    if not attachment_text:
        return []
    # Prefer DD.MM.YY markers (more reliable than day-name spacing in PDF layout)
    date_matches = list(_DATE_MARK_RE.finditer(attachment_text))
    markers: list[tuple[int, date]] = []
    for match in date_matches:
        day = _parse_day_marker_date(match.group("d1"), match.group("m1"), match.group("y1"))
        if day is None:
            continue
        # Keep dates that look like day-row markers (near Mon/Tue or venue lines)
        window = attachment_text[max(0, match.start() - 220) : match.end() + 100]
        if _DAY_LABEL_RE.search(window) or re.search(
            r"Arthum|Tiered|AEC Hall", window, re.I
        ):
            markers.append((match.start(), day))

    # Dedupe by date, keep first occurrence
    seen_dates: set[date] = set()
    unique_markers: list[tuple[int, date]] = []
    for pos, day in markers:
        if day in seen_dates:
            continue
        seen_dates.add(day)
        unique_markers.append((pos, day))
    unique_markers.sort(key=lambda x: x[0])

    chunks: list[tuple[date, str]] = []
    for i, (pos, day) in enumerate(unique_markers):
        # Back up a bit to include the day label / venue column
        start = max(0, pos - 120)
        end = unique_markers[i + 1][0] if i + 1 < len(unique_markers) else len(attachment_text)
        # Don't back up into the previous chunk
        if i > 0:
            start = max(start, unique_markers[i - 1][0])
        chunk = attachment_text[start:end].strip()
        if "sec" in chunk.lower():
            chunks.append((day, chunk))
    return chunks


def _normalize_parsed_sessions(
    parsed: dict[str, Any] | None,
    *,
    force_date: date | None = None,
) -> list[dict[str, Any]]:
    if not parsed or not isinstance(parsed.get("sessions"), list):
        return []
    week_start = parsed.get("week_start")
    sessions: list[dict[str, Any]] = []
    for raw in parsed["sessions"]:
        if not isinstance(raw, dict):
            continue
        session_date = force_date.isoformat() if force_date else raw.get("session_date")
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


def _llm_parse_schedule_chunk(
    *,
    subject: str,
    body: str,
    chunk: str,
    ref: date,
    force_date: date | None = None,
) -> list[dict[str, Any]]:
    system = SCHEDULE_PROMPT.format(today=ref.isoformat())
    date_hint = (
        f"\nThis chunk is ONLY for {force_date.isoformat()} "
        f"({force_date.strftime('%A')}). Set every session_date to that day.\n"
        if force_date
        else ""
    )
    user = (
        f"Subject: {subject}\n\n"
        f"Email body:\n{body[:2000]}\n"
        f"{date_hint}\n"
        "Typical Term-I columns (use these when the chunk is column-jumbled):\n"
        "09:45-11:15, 11:30-13:00 (sometimes 11:30-12:30), 14:00-15:30 "
        "(sometimes 14:00-15:00), 15:45-17:15 (sometimes 15:45-16:45), 18:00-19:30.\n"
        "Map each course to the nearest matching column; include room (Arthum#N / AEC Hall) "
        "and faculty initials in parentheses when present.\n\n"
        f"Timetable text:\n{chunk[:8000]}"
    )
    return _normalize_parsed_sessions(call_json(system, user), force_date=force_date)


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

    day_chunks = _split_timetable_day_chunks(attachment_text or "")
    # #region agent log
    _agent_log(
        "F",
        "schedule.py:parse_schedule_sessions",
        "day chunks detected",
        {
            "chunk_count": len(day_chunks),
            "chunk_dates": [d.isoformat() for d, _ in day_chunks],
            "att_len": len(attachment_text or ""),
        },
    )
    # #endregion

    # Prefer per-day parse when the PDF clearly has multiple dated day blocks
    if len(day_chunks) >= 2:
        merged: list[dict[str, Any]] = []
        for day, chunk in day_chunks:
            part = _llm_parse_schedule_chunk(
                subject=subject,
                body=body,
                chunk=chunk,
                ref=ref,
                force_date=day,
            )
            merged.extend(part)
        # #region agent log
        _agent_log(
            "F",
            "schedule.py:parse_schedule_sessions",
            "per-day parse result",
            {
                "session_count": len(merged),
                "dates": sorted({s.get("session_date") for s in merged if s.get("session_date")}),
            },
        )
        # #endregion
        if merged:
            return merged

    sessions = _llm_parse_schedule_chunk(
        subject=subject,
        body=body,
        chunk=(attachment_text or "")[:10000] or body[:5000],
        ref=ref,
    )
    unique_dates = {s.get("session_date") for s in sessions if s.get("session_date")}
    # Fallback: full parse collapsed to one day but PDF has more day markers
    if len(unique_dates) <= 1 and len(day_chunks) >= 2:
        merged = []
        for day, chunk in day_chunks:
            part = _llm_parse_schedule_chunk(
                subject=subject,
                body=body,
                chunk=chunk,
                ref=ref,
                force_date=day,
            )
            merged.extend(part)
        if merged:
            return merged
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


def _sessions_look_complete(sessions: list[dict[str, Any]], attachment_text: str = "") -> bool:
    """Reject one-day collapses when the PDF clearly spans multiple days."""
    if not sessions:
        return False
    dates = _template_date_set(sessions)
    if len(dates) >= 2:
        return True
    if attachment_text:
        return len(_split_timetable_day_chunks(attachment_text)) < 2
    # Without attachment text we cannot verify — treat single-date as incomplete
    # so refresh reloads the PDF and re-parses.
    return False


def _cached_or_stored_sessions(message_id: str) -> dict[str, Any] | None:
    """Reuse prior parse for the same Gmail message (no LLM)."""
    cached = _parse_cache.get(message_id)
    if cached and cached.get("sessions"):
        if _sessions_look_complete(cached["sessions"], ""):
            return cached
        _parse_cache.pop(message_id, None)
    try:
        stored = get_class_sessions_for_message(message_id)
    except Exception as exc:
        logger.warning("DB lookup for schedule message %s failed: %s", message_id, exc)
        stored = []
    if stored and _sessions_look_complete(stored, ""):
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
        if not _sessions_look_complete(sessions, attachment_text):
            last_error = "parse_incomplete"
            logger.warning(
                "Schedule parse for %s looks incomplete (%d sessions, dates=%s)",
                message_id,
                len(sessions),
                sorted({s.get("session_date") for s in sessions}),
            )
            # Still store what we have, but prefer fuller re-parses next time
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
        stored_dates = _template_date_set(raw)
        # Dated multi-day timetables: show real upcoming calendar dates only
        if len(stored_dates) > 1:
            upcoming = [
                s
                for s in raw
                if (_session_date_value(s) or day) >= day
                and (_session_date_value(s) or end) <= end
            ]
            sessions = filter_by_section(upcoming, section)
        else:
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
    # #region agent log
    _agent_log(
        "D",
        "schedule.py:classes_command_text",
        "template loaded",
        {
            "today": day.isoformat(),
            "section": section,
            "refresh_ok": bool(info.get("ok")),
            "refresh_reason": info.get("reason"),
            "from_cache": info.get("from_cache"),
            "message_id": info.get("message_id"),
            "subject": (info.get("subject") or "")[:120],
            "template_count": len(template),
            "template_dates": sorted(
                {str(s.get("session_date")) for s in template if s.get("session_date")}
            )[:20],
            "template_weekdays": sorted(
                {
                    _session_date_value(s).weekday()
                    for s in template
                    if _session_date_value(s) is not None
                }
            ),
            "sample": [
                {
                    "date": s.get("session_date"),
                    "course": (s.get("course_or_topic") or "")[:60],
                    "start": s.get("start_time"),
                }
                for s in template[:8]
            ],
        },
    )
    # #endregion

    today_text = ""
    try:
        today_text = format_today_classes(day, section=section, template=template)
    except Exception as exc:
        logger.warning("format_today_classes failed: %s", exc)

    if today_text:
        # #region agent log
        _agent_log(
            "E",
            "schedule.py:classes_command_text",
            "returned today_text",
            {"path": "today", "len": len(today_text)},
        )
        # #endregion
        return today_text + _source_footer(info)

    if template:
        upcoming_text = format_upcoming_classes(day, days=7, section=section, template=template)
        if upcoming_text:
            # #region agent log
            _agent_log(
                "E",
                "schedule.py:classes_command_text",
                "returned upcoming_text",
                {"path": "upcoming", "len": len(upcoming_text)},
            )
            # #endregion
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
