"""IST-aware deadline parsing and normalization."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

IST = ZoneInfo("Asia/Kolkata")

_WEEKDAY_MAP = {
    "monday": MO,
    "mon": MO,
    "tuesday": TU,
    "tue": TU,
    "tues": TU,
    "wednesday": WE,
    "wed": WE,
    "thursday": TH,
    "thu": TH,
    "thurs": TH,
    "friday": FR,
    "fri": FR,
    "saturday": SA,
    "sat": SA,
    "sunday": SU,
    "sun": SU,
}

_VAGUE_PHRASES = (
    "sometime",
    "soon",
    "asap",
    "as soon as possible",
    "next week",
    "coming week",
    "in a few days",
)


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def to_storage_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat()


def parse_iso_deadline(value: str | None) -> datetime | None:
    """Parse LLM/storage deadline strings; naive values are interpreted as IST."""
    if not value or not str(value).strip():
        return None

    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = date_parser.parse(raw, dayfirst=True, fuzzy=False)
        except (ValueError, TypeError, OverflowError):
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    else:
        dt = dt.astimezone(IST)

    # Date-only input defaults to end of day IST
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.combine(dt.date(), time(23, 59), tzinfo=IST)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", raw):
        if dt.hour == 0 and dt.minute == 0:
            dt = datetime.combine(dt.date(), time(23, 59), tzinfo=IST)

    return dt


def _extract_time(text: str) -> tuple[int, int] | None:
    lower = text.lower()
    m = re.search(r"(\d{1,2})\s*:\s*(\d{2})\s*(am|pm)?", lower)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        meridiem = m.group(3)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return hour, minute

    m = re.search(r"(\d{1,2})\s*(am|pm)", lower)
    if m:
        hour = int(m.group(1))
        meridiem = m.group(2)
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        return hour, 0

    if re.search(r"\beod\b|end of day|midnight|by tonight|tonight", lower):
        return 23, 59
    if re.search(r"\bnoon\b", lower):
        return 12, 0
    return None


def _is_vague(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _VAGUE_PHRASES)


def resolve_deadline_text(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Resolve relative/common deadline phrases to an IST datetime."""
    if not text or not str(text).strip():
        return None

    raw = str(text).strip()
    if _is_vague(raw):
        return None

    ref = now.astimezone(IST) if now else now_ist()
    lower = raw.lower()

    if re.search(r"\btoday\b", lower):
        base = ref.date()
    elif re.search(r"\btomorrow\b", lower):
        base = (ref + timedelta(days=1)).date()
    elif match := re.search(r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", lower):
        weekday = _WEEKDAY_MAP[match.group(1)]
        base = (ref + relativedelta(weekday=weekday(+1))).date()
    elif match := re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b", lower):
        weekday = _WEEKDAY_MAP[match.group(1)]
        base = (ref + relativedelta(weekday=weekday(+1))).date()
    elif re.search(r"\bend of week\b|\bthis week\b|\bby friday\b", lower):
        base = (ref + relativedelta(weekday=FR(+1))).date()
    else:
        try:
            parsed = date_parser.parse(raw, default=ref.replace(tzinfo=None), dayfirst=True, fuzzy=True)
            base = parsed.date()
        except (ValueError, TypeError, OverflowError):
            return None

    hour, minute = _extract_time(raw) or (23, 59)
    return datetime.combine(base, time(hour, minute), tzinfo=IST)


def resolve_deadline(
    deadline_date: str | None,
    deadline_text: str | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return storage ISO for the best available deadline."""
    parsed = parse_iso_deadline(deadline_date)
    if parsed is None and deadline_text:
        parsed = resolve_deadline_text(deadline_text, now=now)
    if parsed is None:
        return None
    return to_storage_iso(parsed)


def deadline_local_date(value: str | None, tz: ZoneInfo = IST) -> date | None:
    dt = parse_iso_deadline(value)
    if dt is None:
        return None
    return dt.astimezone(tz).date()


def item_effective_deadline_dt(
    item: dict[str, Any],
    *,
    now: datetime | None = None,
) -> datetime | None:
    parsed = parse_iso_deadline(item.get("deadline_date"))
    if parsed is not None:
        return parsed
    return resolve_deadline_text(item.get("deadline_text"), now=now)


def format_deadline_display(
    deadline_date: str | None,
    deadline_text: str | None,
    *,
    tz: ZoneInfo = IST,
) -> str:
    dt = parse_iso_deadline(deadline_date)
    if dt is None and deadline_text:
        dt = resolve_deadline_text(deadline_text)
    if dt is not None:
        formatted = dt.astimezone(tz).strftime("%a, %d %b %Y %I:%M %p")
        if deadline_text and deadline_text.strip():
            return f"{formatted} ({deadline_text.strip()})"
        return formatted
    if deadline_text and deadline_text.strip():
        return deadline_text.strip()
    return "Not specified"
