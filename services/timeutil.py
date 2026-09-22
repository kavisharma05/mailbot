"""IST-aware deadline parsing and normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

IST = ZoneInfo("Asia/Kolkata")

# GitHub cron can arrive hours late. Digests only send inside these IST windows
# unless the caller passes force=1.
MORNING_DIGEST_HOURS = range(7, 9)  # 07:00–08:59
EVENING_DIGEST_HOURS = range(20, 22)  # 20:00–21:59


def digest_window_open(kind: str, now: datetime | None = None) -> bool:
    """True when a scheduled digest is allowed to send."""
    now = now or datetime.now(IST)
    if kind == "morning":
        return now.hour in MORNING_DIGEST_HOURS
    if kind == "evening":
        return now.hour in EVENING_DIGEST_HOURS
    if kind == "library":
        minutes = now.hour * 60 + now.minute
        return (7 * 60 + 15) <= minutes < (10 * 60)
    return False

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

_DATE_TYPO_MAP: dict[str, str] = {
    "tmrw": "tomorrow",
    "tmr": "tomorrow",
    "tmw": "tomorrow",
    "2moro": "tomorrow",
    "2morow": "tomorrow",
    "tomo": "tomorrow",
    "tomm": "tomorrow",
    "tommorow": "tomorrow",
    "tommorrow": "tomorrow",
    "tdy": "today",
    "tday": "today",
    "todays": "today",
    "yest": "yesterday",
    "ystrday": "yesterday",
    "yesterday's": "yesterday",
}

_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


@dataclass(frozen=True)
class DayWindow:
    """Inclusive calendar-day range in IST."""

    start: date
    end: date
    label: str
    single: bool


def normalize_date_words(text: str) -> str:
    """Replace common date typos/shorthand with canonical words."""
    if not text:
        return text
    result = text
    for typo, canonical in _DATE_TYPO_MAP.items():
        result = re.sub(rf"\b{re.escape(typo)}\b", canonical, result, flags=re.IGNORECASE)
    return result


def _format_day_label(day: date) -> str:
    return day.strftime("%a, %d %b %Y")


def _this_week_end(ref: date) -> date:
    return ref + timedelta(days=(6 - ref.weekday()))


def _next_week_window(ref: date) -> tuple[date, date]:
    this_sunday = _this_week_end(ref)
    start = this_sunday + timedelta(days=1)
    end = start + timedelta(days=6)
    return start, end


def _parse_explicit_date(q: str, ref: date) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:of\s+)?"
        r"(jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        r"jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december))?\b",
        q,
    )
    if not m:
        return None
    if not m.group(2) and not any(
        token in q
        for token in (
            "deadline",
            "due",
            "class",
            "classes",
            "schedule",
            "mail",
            "email",
            "for ",
            "on ",
        )
    ):
        return None
    try:
        dom = int(m.group(1))
        if m.group(2):
            month = _MONTHS[m.group(2)]
            year = ref.year
        else:
            month = ref.month
            year = ref.year
            if dom < ref.day:
                month += 1
                if month > 12:
                    month = 1
                    year += 1
        return date(year, month, dom)
    except ValueError:
        return None


def _parse_weekday(q: str, ref: date) -> date | None:
    m = re.search(
        r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"mon|tue|tues|wed|thu|thurs|fri|sat|sun)\b",
        q,
    )
    if m:
        weekday = _WEEKDAY_MAP[m.group(1)]
        return (ref + relativedelta(weekday=weekday(+1))).date()

    for name, weekday in _WEEKDAY_MAP.items():
        if re.search(rf"\b{re.escape(name)}\b", q):
            days_ahead = (weekday.weekday - ref.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return ref + timedelta(days=days_ahead)
    return None


def parse_day_window(text: str, *, ref: datetime | None = None) -> DayWindow | None:
    """Resolve natural-language day/range phrases to an IST calendar window."""
    if not text or not str(text).strip():
        return None

    ref_dt = ref.astimezone(IST) if ref else now_ist()
    ref_day = ref_dt.date()
    q = normalize_date_words(text).lower()

    if re.search(r"\btoday\b.*\btomorrow\b|\btomorrow\b.*\btoday\b", q):
        end = ref_day + timedelta(days=1)
        return DayWindow(ref_day, end, "today and tomorrow (IST)", single=False)

    if re.search(r"\bthis week\b", q):
        end = _this_week_end(ref_day)
        return DayWindow(ref_day, end, "this week (IST)", single=False)

    if re.search(r"\bnext week\b|\bcoming week\b", q):
        start, end = _next_week_window(ref_day)
        return DayWindow(start, end, "next week (IST)", single=False)

    if re.search(r"\bday after tomorrow\b", q):
        target = ref_day + timedelta(days=2)
        return DayWindow(target, target, _format_day_label(target), single=True)

    if re.search(r"\btoday\b", q):
        return DayWindow(ref_day, ref_day, "today (IST)", single=True)

    if re.search(r"\btomorrow\b", q):
        target = ref_day + timedelta(days=1)
        return DayWindow(target, target, "tomorrow (IST)", single=True)

    if re.search(r"\byesterday\b", q):
        target = ref_day - timedelta(days=1)
        return DayWindow(target, target, "yesterday (IST)", single=True)

    explicit = _parse_explicit_date(q, ref_day)
    if explicit is not None:
        return DayWindow(explicit, explicit, _format_day_label(explicit), single=True)

    weekday = _parse_weekday(q, ref_day)
    if weekday is not None:
        return DayWindow(weekday, weekday, _format_day_label(weekday), single=True)

    return None


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
    elif match := re.search(
        r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        lower,
    ):
        weekday = _WEEKDAY_MAP[match.group(1)]
        base = (ref + relativedelta(weekday=weekday(+1))).date()
    elif match := re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        lower,
    ):
        weekday = _WEEKDAY_MAP[match.group(1)]
        days_ahead = (weekday.weekday - ref.weekday()) % 7
        base = (ref + timedelta(days=days_ahead)).date()
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
    """Prefer stored deadline_date; else resolve deadline_text from created_at (not wall clock)."""
    parsed = parse_iso_deadline(item.get("deadline_date"))
    if parsed is not None:
        return parsed

    ref = now
    if ref is None and item.get("created_at"):
        ref = parse_iso_deadline(str(item["created_at"]))
    # Merge summary time if deadline_text is just "tomorrow"/"today"
    text = item.get("deadline_text")
    summary = item.get("summary")
    if text and summary and not _extract_time(str(text)) and _extract_time(str(summary)):
        text = f"{text}; {summary}"
    return resolve_deadline_text(text, now=ref)


_STALE_RELATIVE_NOTE = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|tmrw)\b",
    re.IGNORECASE,
)


def _deadline_note(deadline_text: str | None) -> str:
    """Keep a useful extracted note, but drop phrases that go stale.

    Classifiers store "tomorrow 11:30 PM" next to an absolute date. Weeks later
    that parenthetical still says tomorrow, which makes digests unreadable.
    """
    note = (deadline_text or "").strip()
    if not note or _STALE_RELATIVE_NOTE.search(note):
        return ""
    if len(note) > 80:
        note = note[:77].rstrip(" ,.;") + "…"
    return note


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
        local = dt.astimezone(tz)
        formatted = local.strftime("%a, %d %b %Y %I:%M %p")
        now = now_ist()
        delta = local - now
        relative = ""
        if timedelta(0) < delta <= timedelta(hours=48):
            total_minutes = int(delta.total_seconds() // 60)
            if total_minutes < 60:
                relative = f"Due in {total_minutes}m"
            elif total_minutes < 24 * 60:
                hours = total_minutes // 60
                mins = total_minutes % 60
                relative = f"Due in {hours}h" + (f" {mins}m" if mins else "")
            else:
                days = total_minutes // (24 * 60)
                hours = (total_minutes % (24 * 60)) // 60
                relative = f"Due in {days}d" + (f" {hours}h" if hours else "")
        elif delta <= timedelta(0) and delta >= timedelta(hours=-36):
            overdue_mins = int(abs(delta.total_seconds()) // 60)
            if overdue_mins < 60:
                relative = f"Overdue {overdue_mins}m ago"
            else:
                hours = overdue_mins // 60
                relative = f"Overdue {hours}h ago" if hours < 48 else "Overdue"
        elif delta < timedelta(hours=-36):
            relative = "Overdue"
        if relative:
            formatted = f"{relative} — {formatted}"
        note = _deadline_note(deadline_text)
        if note:
            return f"{formatted} ({note})"
        return formatted
    if deadline_text and deadline_text.strip():
        return deadline_text.strip()
    return "Not specified"
