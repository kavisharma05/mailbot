"""Parse and store college class timetables from Gmail (body + PDF/Excel)."""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any

from ai.client import call_json
from ai.prompts import SCHEDULE_PROMPT
from gmail_api.client import get_message_details, search_message_ids
from config import get_settings
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
        return sorted(exact, key=lambda x: x.get("start_time") or "")

    stored_dates = _template_date_set(sessions)
    stored_weekdays = {d.weekday() for d in stored_dates}
    # Single-day (or single-weekday) dumps are dated snapshots — do not project
    if len(stored_weekdays) <= 1:
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
# Skip another Gmail search when /classes was just refreshed (Render free tier
# plus several Gmail queries made every tap feel slow).
_SCHEDULE_REFRESH_TTL_SECONDS = 15 * 60
_last_gmail_refresh_mono: float = 0.0


def schedule_cache_is_fresh() -> bool:
    return (
        _last_gmail_refresh_mono > 0
        and (time.monotonic() - _last_gmail_refresh_mono) < _SCHEDULE_REFRESH_TTL_SECONDS
    )


def _mark_schedule_refreshed() -> None:
    global _last_gmail_refresh_mono
    _last_gmail_refresh_mono = time.monotonic()

# Newest-first Gmail searches for timetable mails (incl. PDF attachments)
_SCHEDULE_QUERIES_BASE = (
    'subject:(timetable OR "time table" OR "class schedule" OR "weekly schedule" OR "class timings") newer_than:90d',
    '("timetable" OR "time table" OR "class schedule" OR "weekly schedule") has:attachment newer_than:90d',
    '(filename:timetable OR filename:schedule OR filename:"time table") newer_than:90d',
    'subject:(MBA OR PGDM OR "batch") (timetable OR schedule OR classroom) newer_than:60d',
)


def _schedule_queries() -> tuple[str, ...]:
    settings = get_settings()
    allowed = [s.strip() for s in settings.admin_schedule_sender.split(",") if s.strip()]
    if not allowed:
        return _SCHEDULE_QUERIES_BASE
    from_clause = " OR ".join(f"from:{a}" for a in allowed)
    return tuple(f"({from_clause}) {q}" for q in _SCHEDULE_QUERIES_BASE)


def is_trusted_schedule_sender(sender: str) -> bool:
    """Return True if sender is allowed to update class_sessions."""
    settings = get_settings()
    allowed = [s.strip().lower() for s in settings.admin_schedule_sender.split(",") if s.strip()]
    if not allowed:
        if settings.require_trusted_schedule_sender and (settings.render_url or "").strip():
            return False
        return True
    sender_lower = (sender or "").lower()
    return any(a in sender_lower for a in allowed)


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


_TIME_RANGE_RE = re.compile(
    r"(?P<h1>\d{1,2})[.:](?P<m1>\d{2})\s*(?P<p1>am|pm)"
    r"\s*-\s*"
    r"(?P<h2>\d{1,2})[.:](?P<m2>\d{2})\s*(?P<p2>am|pm)?",
    re.IGNORECASE,
)
_COURSE_CELL_RE = re.compile(
    r"(?P<course>[A-Z][A-Z0-9/-]{1,8})\s*-\s*Sec\.?\s*(?P<section>[A-Za-z])\s*-\s*(?P<slot>\d+)",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(
    r"(Arthum\s*#?\s*\d+|Tiered\s*#?\s*\d+|AEC\s*Hall|LH\s*\d+|CR\s*\d+)",
    re.IGNORECASE,
)
_FACULTY_RE = re.compile(r"\(\s*([A-Z]{1,4})\s*\)")


def _to_hhmm(hour: int, minute: int, meridiem: str | None) -> str:
    h = hour
    mer = (meridiem or "").lower()
    if mer == "pm" and h < 12:
        h += 12
    if mer == "am" and h == 12:
        h = 0
    return f"{h:02d}:{minute:02d}"


def _parse_time_range(text: str) -> tuple[str, str] | None:
    m = _TIME_RANGE_RE.search(text)
    if not m:
        return None
    start = _to_hhmm(int(m.group("h1")), int(m.group("m1")), m.group("p1"))
    end_mer = m.group("p2") or m.group("p1")
    end = _to_hhmm(int(m.group("h2")), int(m.group("m2")), end_mer)
    return start, end


def _extract_time_columns(attachment_text: str) -> list[dict[str, Any]]:
    """Read the PDF header row into ordered time columns with character anchors."""
    defaults = [
        {"start_col": 30, "start_time": "09:45", "end_time": "11:15"},
        {"start_col": 59, "start_time": "11:30", "end_time": "13:00"},
        {"start_col": 102, "start_time": "14:00", "end_time": "15:30"},
        {"start_col": 134, "start_time": "15:45", "end_time": "17:15"},
        {"start_col": 164, "start_time": "18:00", "end_time": "19:30"},
    ]
    header = ""
    for line in (attachment_text or "").splitlines():
        if re.search(r"\d{1,2}[.:]\d{2}\s*am", line, re.I) and (
            "venue" in line.lower() or line.lower().count("am") + line.lower().count("pm") >= 3
        ):
            header = line
            break
    if not header:
        return defaults

    cols: list[dict[str, Any]] = []
    for m in _TIME_RANGE_RE.finditer(header):
        start = _to_hhmm(int(m.group("h1")), int(m.group("m1")), m.group("p1"))
        end_mer = m.group("p2") or m.group("p1")
        end = _to_hhmm(int(m.group("h2")), int(m.group("m2")), end_mer)
        # Skip lunch / break fragments (e.g. 1.00pm–2.00pm)
        if start == "13:00":
            continue
        cols.append({"start_col": m.start(), "start_time": start, "end_time": end})

    # Header often splits "1.00pm -" / "2.00pm - 3.30pm"; ensure afternoon slot exists
    if cols and not any(c["start_time"] == "14:00" for c in cols):
        m2 = re.search(r"2[.:]00\s*pm\s*-\s*3[.:]30\s*pm", header, re.I)
        if m2:
            cols.append(
                {"start_col": m2.start(), "start_time": "14:00", "end_time": "15:30"}
            )
    cols.sort(key=lambda c: c["start_col"])
    return cols or defaults


def _column_for_offset(columns: list[dict[str, Any]], col: int) -> dict[str, Any] | None:
    if not columns:
        return None
    # Midpoints between column starts
    best = columns[0]
    best_dist = abs(col - int(best["start_col"]))
    for c in columns[1:]:
        dist = abs(col - int(c["start_col"]))
        if dist < best_dist:
            best = c
            best_dist = dist
    return best


def _find_course_in_chunk(chunk: str, course: str) -> re.Match[str] | None:
    cell = _COURSE_CELL_RE.search(course or "")
    if cell:
        pat = re.compile(
            rf"{re.escape(cell.group('course'))}\s*-\s*Sec\.?\s*"
            rf"{re.escape(cell.group('section'))}\s*-\s*{re.escape(cell.group('slot'))}",
            re.IGNORECASE,
        )
        hit = pat.search(chunk)
        if hit:
            return hit
    needle = re.sub(r"\s+", r"\\s*", re.escape((course or "").strip()[:40]))
    if needle:
        hit = re.search(needle, chunk, re.IGNORECASE)
        if hit:
            return hit
    return None


def _nearby_override_time(chunk: str, abs_pos: int, col: int) -> tuple[str, str] | None:
    """Prefer a short override printed in the same column near the course."""
    line_start = chunk.rfind("\n", 0, abs_pos) + 1
    line_end = chunk.find("\n", abs_pos)
    if line_end < 0:
        line_end = len(chunk)
    # Search this line and the next 2 lines in a column window
    region_end = line_end
    for _ in range(2):
        nxt = chunk.find("\n", region_end + 1)
        if nxt < 0:
            region_end = len(chunk)
            break
        region_end = nxt
    region = chunk[line_start:region_end]
    best: tuple[str, str] | None = None
    best_dist = 10**9
    for m in _TIME_RANGE_RE.finditer(region):
        # Approximate column within the multi-line region by position on its line
        abs_m = line_start + m.start()
        m_line_start = chunk.rfind("\n", 0, abs_m) + 1
        m_col = abs_m - m_line_start
        dist = abs(m_col - col)
        if dist > 28:
            continue
        parsed = _parse_time_range(m.group(0))
        if parsed and dist < best_dist:
            best = parsed
            best_dist = dist
    return best


def _nearby_room(chunk: str, abs_pos: int) -> str | None:
    line_start = chunk.rfind("\n", 0, abs_pos) + 1
    # Venue is usually on the same row or the next line, left side
    line_end = chunk.find("\n", abs_pos)
    if line_end < 0:
        line_end = len(chunk)
    window = chunk[max(0, line_start - 80) : min(len(chunk), line_end + 120)]
    m = _ROOM_RE.search(window)
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    raw = re.sub(r"(Arthum|Tiered)\s*#\s*", r"\1#", raw, flags=re.I)
    return raw


def _correct_session_times_from_layout(
    sessions: list[dict[str, Any]],
    *,
    chunk: str,
    columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Snap LLM session times to PDF layout columns (character position)."""
    if not sessions or not chunk or not columns:
        return sessions
    corrected: list[dict[str, Any]] = []
    for session in sessions:
        course = str(session.get("course_or_topic") or "")
        hit = _find_course_in_chunk(chunk, course)
        if not hit:
            corrected.append(session)
            continue
        line_start = chunk.rfind("\n", 0, hit.start()) + 1
        col = hit.start() - line_start
        slot = _column_for_offset(columns, col)
        override = _nearby_override_time(chunk, hit.start(), col)
        start_time, end_time = override if override else (
            (slot["start_time"], slot["end_time"]) if slot else (None, None)
        )
        updated = {**session}
        if start_time:
            updated["start_time"] = start_time
        if end_time:
            updated["end_time"] = end_time
        if not updated.get("room"):
            room = _nearby_room(chunk, hit.start())
            if room:
                updated["room"] = room
        # Faculty under the course, same column
        if not updated.get("faculty") or str(updated.get("faculty")).startswith("("):
            line_end = chunk.find("\n", hit.start())
            if line_end < 0:
                line_end = len(chunk)
            next_end = chunk.find("\n", line_end + 1)
            if next_end < 0:
                next_end = len(chunk)
            under = chunk[line_end:next_end]
            # faculty on next line near same column
            for fm in _FACULTY_RE.finditer(under):
                f_col = (line_end + 1 + fm.start()) - (line_end + 1)
                # fm.start() relative to under; under starts at line_end
                abs_f = line_end + fm.start()
                f_line_start = chunk.rfind("\n", 0, abs_f) + 1
                f_col = abs_f - f_line_start
                if abs(f_col - col) <= 30:
                    updated["faculty"] = fm.group(1)
                    break
        corrected.append(updated)
    return corrected


def _correct_sessions_against_pdf(
    sessions: list[dict[str, Any]],
    attachment_text: str,
) -> list[dict[str, Any]]:
    """Re-align all sessions to PDF column times using the full attachment text."""
    if not sessions or not attachment_text:
        return sessions
    columns = _extract_time_columns(attachment_text)
    day_chunks = {d: c for d, c in _split_timetable_day_chunks(attachment_text)}
    out: list[dict[str, Any]] = []
    for session in sessions:
        day = _session_date_value(session)
        chunk = day_chunks.get(day) if day else None
        if not chunk:
            chunk = attachment_text
        fixed = _correct_session_times_from_layout(
            [session], chunk=chunk, columns=columns
        )[0]
        out.append(fixed)
    return out


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
        faculty = raw.get("faculty")
        if isinstance(faculty, str):
            faculty = faculty.strip().strip("()")
        sessions.append(
            {
                "course_or_topic": str(course)[:200],
                "session_date": str(session_date)[:10],
                "start_time": str(start)[:5],
                "end_time": (str(raw["end_time"])[:5] if raw.get("end_time") else None),
                "room": (str(raw["room"])[:120] if raw.get("room") else None),
                "faculty": (str(faculty)[:120] if faculty else None),
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
    columns: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    system = SCHEDULE_PROMPT.format(today=ref.isoformat())
    date_hint = (
        f"\nThis chunk is ONLY for {force_date.isoformat()} "
        f"({force_date.strftime('%A')}). Set every session_date to that day.\n"
        if force_date
        else ""
    )
    col_hint = ""
    if columns:
        parts = [
            f"{c['start_time']}-{c['end_time']} (text col ~{c['start_col']})"
            for c in columns
        ]
        col_hint = "Detected PDF time columns (left→right): " + " | ".join(parts) + "\n"
    user = (
        f"Subject: {subject}\n\n"
        f"Email body:\n{body[:2000]}\n"
        f"{date_hint}"
        f"{col_hint}\n"
        "Use horizontal position in the layout text to pick the correct column times. "
        "Courses on the right are later. Include room (Arthum#N / AEC Hall / Tiered#N) "
        "and faculty initials.\n\n"
        f"Timetable text (layout-preserved from PDF):\n{chunk[:8000]}"
    )
    # Full-week timetables can produce long JSON — give the model room.
    sessions = _normalize_parsed_sessions(
        call_json(system, user, max_tokens=2200), force_date=force_date
    )
    return _correct_session_times_from_layout(
        sessions, chunk=chunk, columns=columns or _extract_time_columns(chunk)
    )


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
    columns = _extract_time_columns(attachment_text or "")

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
                columns=columns,
            )
            merged.extend(part)
        if merged:
            return _correct_sessions_against_pdf(merged, attachment_text or "")

    sessions = _llm_parse_schedule_chunk(
        subject=subject,
        body=body,
        chunk=(attachment_text or "")[:10000] or body[:5000],
        ref=ref,
        columns=columns,
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
                columns=columns,
            )
            merged.extend(part)
        if merged:
            return _correct_sessions_against_pdf(merged, attachment_text or "")
    return _correct_sessions_against_pdf(sessions, attachment_text or "")


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


def _next_day_with_classes(
    sessions: list[dict[str, Any]], from_day: date
) -> tuple[date | None, list[dict[str, Any]]]:
    """Find the next calendar day (within 7 days) that has sessions."""
    by_day: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        by_day.setdefault(s["session_date"], []).append(s)
    for offset in range(1, 8):
        candidate_date = from_day + timedelta(days=offset)
        candidate_key = candidate_date.isoformat()
        if candidate_key in by_day:
            return candidate_date, by_day[candidate_key]
    return None, []


def find_latest_schedule_message_ids(*, max_candidates: int = 8) -> list[str]:
    """Return newest-ish candidate message ids for timetable emails."""
    seen: list[str] = []
    for query in _schedule_queries():
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


def _session_time_fingerprint(sessions: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (
            str(s.get("session_date") or ""),
            str(s.get("course_or_topic") or ""),
            str(s.get("start_time") or ""),
        )
        for s in sessions
    }


def _realign_cached_with_pdf(message_id: str, info: dict[str, Any]) -> dict[str, Any]:
    """Reload PDF layout and snap cached session times to columns (no LLM)."""
    try:
        details = get_message_details(message_id, load_attachments=True)
    except Exception as exc:
        logger.warning("Could not reload PDF for time realign %s: %s", message_id, exc)
        return info
    attachment_text = details.get("attachment_text") or ""
    if not attachment_text:
        return info
    original = list(info.get("sessions") or [])
    corrected = _correct_sessions_against_pdf(original, attachment_text)
    if _session_time_fingerprint(corrected) != _session_time_fingerprint(original):
        logger.info("Realigning schedule times from PDF layout for %s", message_id)
        stored = store_schedule_sessions(corrected, gmail_message_id=message_id)
        info = {
            **info,
            "sessions": stored or corrected,
            "from_cache": False,
            "times_realigned": True,
            "subject": info.get("subject") or details.get("subject"),
            "received_at": info.get("received_at") or details.get("received_at"),
            "had_attachment_text": True,
        }
        _parse_cache[message_id] = info
    return info


def refresh_schedule_from_gmail() -> dict[str, Any]:
    """
    Find the latest timetable mail, read body + PDF/Excel, parse sessions, store them.
    Returns metadata + sessions (even if DB insert fails).
    Skips LLM when this message was already parsed successfully, but still
    realigns times from the PDF layout columns.
    """
    candidates = find_latest_schedule_message_ids()
    if not candidates:
        return {"ok": False, "reason": "no_mail", "sessions": []}

    last_error = ""
    for message_id in candidates:
        reused = _cached_or_stored_sessions(message_id)
        if reused:
            logger.info("Reusing cached schedule parse for message %s", message_id)
            _mark_schedule_refreshed()
            return _realign_cached_with_pdf(message_id, reused)

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
        _mark_schedule_refreshed()
        return info

    reason = last_error or "no_parse"
    return {"ok": False, "reason": reason, "sessions": []}


def format_schedule_share(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "No class sessions could be extracted from this timetable."

    by_day: dict[str, list[dict[str, Any]]] = {}
    for s in sessions:
        day_key = s.get("session_date") or "unknown"
        by_day.setdefault(day_key, []).append(s)

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
    if refresh and schedule_cache_is_fresh():
        cached = load_template_sessions()
        if cached:
            refresh = False
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
        week_end = day + timedelta(days=7)
        raw_sessions = info.get("sessions") or template
        if not raw_sessions:
            try:
                raw_sessions = get_classes_between(day.isoformat(), week_end.isoformat())
            except Exception:
                raw_sessions = []
        sessions = filter_by_section(raw_sessions, section)
        next_day, next_sessions = _next_day_with_classes(sessions, day)
        no_today_line = f"No classes today ({day.strftime('%a, %d %b')})."
        if next_day:
            label = next_day.strftime("%a, %d %b %Y")
            body = _format_session_block(next_sessions, f"<b>📚 Next classes — {label}</b>")
            return f"{no_today_line}\n\n{body}" + _source_footer(info)
        return (
            f"{no_today_line}\n"
            "No upcoming classes found in the latest timetable either."
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
