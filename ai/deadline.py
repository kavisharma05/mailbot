from datetime import datetime
from typing import Any
import re

from ai.client import call_json
from ai.prompts import DEADLINE_PROMPT, DEADLINE_REPAIR_PROMPT
from services.schedule import looks_like_schedule_change
from services.timeutil import (
    IST,
    _extract_time,
    now_ist,
    parse_iso_deadline,
    resolve_deadline,
    today_ist,
)

VALID_CATEGORIES = ("deadline", "placement", "certification", "admin", "other")
VALID_CONFIDENCE = ("high", "medium", "low")
VALID_ITEM_TYPES = ("assignment", "exam", "fee", "form", "other")
VALID_PRIORITIES = ("high", "normal", "low")


def _normalize_category(value: str | None) -> str:
    if value in VALID_CATEGORIES:
        return value
    return "other"


_ACADEMIC_SIGNALS = (
    "presentation",
    "group presentation",
    "class presentation",
    "assignment",
    "case study",
    "for class",
    "for tomorrow",
    "tomorrow's session",
    "session prep",
    "prepare chapter",
    "quiz",
    "midterm",
    "mid-term",
    "end-term",
    "end term",
    "viva",
    "project report",
    "group project",
    "class prep",
    "reading for",
)

_STRONG_CERT_SIGNALS = (
    "nse certification",
    "nse academy",
    "bloomberg certification",
    "bloomberg market concepts",
    "coursera",
    "linkedin learning",
    "skill certification",
    "professional certification",
    "certification course",
    "certificate course",
    "complete your certification",
    "mandatory certification",
)

_WEAK_CERT_SIGNALS = (
    "certification",
    "certificate",
    "certified",
)

_PLACEMENT_SIGNALS = (
    "shortlist",
    "placement",
    "recruit",
    "interview slot",
    "cv deadline",
    "resume deadline",
    "company ppt",
    "job opening",
)


def _blob_has(blob: str, signals: tuple[str, ...]) -> bool:
    for s in signals:
        if " " in s:
            if s in blob:
                return True
        elif re.search(rf"\b{re.escape(s)}\b", blob):
            return True
    return False


def correct_category_from_text(
    category: str | None,
    *texts: str | None,
) -> str:
    """Fix certification false positives on academic coursework (presentations, cases)."""
    cat = _normalize_category(category)
    blob = "\n".join(t for t in texts if t).lower()
    if not blob:
        return cat
    academic = _blob_has(blob, _ACADEMIC_SIGNALS)
    strong_cert = _blob_has(blob, _STRONG_CERT_SIGNALS)
    if cat == "certification" and academic and not strong_cert:
        return "deadline"
    return cat


def _boost_category_from_signals(
    subject: str,
    body: str,
    sender: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Lightweight keyword/sender corrections for common MBA mail types."""
    task_bits = f"{result.get('task') or ''}\n{result.get('summary') or ''}\n{result.get('course_or_topic') or ''}"
    blob = f"{subject}\n{body}\n{sender}\n{task_bits}".lower()
    category = _normalize_category(result.get("category"))
    academic = _blob_has(blob, _ACADEMIC_SIGNALS)
    strong_cert = _blob_has(blob, _STRONG_CERT_SIGNALS)
    weak_cert = _blob_has(blob, _WEAK_CERT_SIGNALS)

    # Class presentations / assignments are coursework, not professional certifications.
    # Lone words like "certificate" or "certified" appear in academic mail too often.
    if category == "certification" and academic and not strong_cert:
        result["category"] = "deadline"
    elif strong_cert and not academic:
        result["category"] = "certification"
        if result.get("is_actionable") is not False:
            result["is_actionable"] = True
        if result.get("confidence") == "low":
            result["confidence"] = "medium"
    elif weak_cert and not academic and category in ("other", "admin"):
        result["category"] = "certification"
        if result.get("is_actionable") is not False:
            result["is_actionable"] = True
        if result.get("confidence") == "low":
            result["confidence"] = "medium"
    elif _blob_has(blob, _PLACEMENT_SIGNALS) and category in ("other", "admin"):
        result["category"] = "placement"
        if result.get("confidence") == "low":
            result["confidence"] = "medium"

    # Faculty class cancel / venue / extra-class notices are admin schedule updates.
    if looks_like_schedule_change(subject, body) and category in ("other", "deadline", "certification"):
        result["category"] = "admin"
        result["is_actionable"] = True
        if result.get("confidence") == "low":
            result["confidence"] = "medium"

    if not result.get("sender") and sender:
        result["sender"] = sender.split("<")[0].strip().strip('"')

    return result


def _normalize_item_type(value: str | None) -> str:
    if value in VALID_ITEM_TYPES:
        return value
    return "other"


def _normalize_priority(value: str | None) -> str:
    if value in VALID_PRIORITIES:
        return value
    return "normal"


def _boost_priority_from_signals(
    subject: str,
    body: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    blob = f"{subject}\n{body}\n{result.get('task') or ''}\n{result.get('summary') or ''}".lower()
    priority = _normalize_priority(result.get("priority"))
    high_signals = (
        "weightage",
        "weightage:",
        "mandatory",
        "compulsory",
        "graded",
        "final exam",
        "midterm",
        "mid-term",
        "must submit",
        "critical",
    )
    low_signals = ("optional", "if interested", "fyi", "no penalty")
    if any(s in blob for s in high_signals):
        result["priority"] = "high"
    elif any(s in blob for s in low_signals):
        result["priority"] = "low"
    elif result.get("category") in ("placement", "certification"):
        result["priority"] = "high"
    else:
        result["priority"] = priority
    return result


def _infer_item_type(result: dict[str, Any]) -> str:
    item_type = _normalize_item_type(result.get("item_type"))
    if item_type != "other":
        return item_type
    blob = f"{result.get('task') or ''} {result.get('summary') or ''}".lower()
    category = result.get("category")
    if any(w in blob for w in ("exam", "quiz", "midterm", "final")):
        return "exam"
    if any(w in blob for w in ("fee", "payment", "remittance")):
        return "fee"
    if any(w in blob for w in ("form", "registration", "register")):
        return "form"
    if any(w in blob for w in ("presentation", "assignment", "case", "project", "viva")):
        return "assignment"
    if category == "deadline":
        return "assignment"
    return "other"


def _normalize_confidence(value: str | None) -> str:
    if value in VALID_CONFIDENCE:
        return value
    return "low"


def _reference_now(received_at: datetime | str | None) -> datetime:
    """Use email received time for relative phrases like 'tomorrow'."""
    if received_at is None:
        return now_ist()
    if isinstance(received_at, datetime):
        dt = received_at
    else:
        dt = parse_iso_deadline(str(received_at))
        if dt is None:
            return now_ist()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _first_time_phrase(*texts: str | None) -> str | None:
    """Pull a clock time like '9:45 am' from summary/body for session deadlines."""
    import re

    for text in texts:
        if not text:
            continue
        m = re.search(
            r"(\d{1,2}\s*:\s*\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm))",
            text,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
    return None


def _enrich_deadline_text(
    deadline_text: str | None,
    summary: str | None,
    body: str | None = None,
) -> str | None:
    """If deadline_text has no clock time, pull it from summary or email body (e.g. 9:45)."""
    if not deadline_text:
        # Still try to keep a relative anchor if body mentions tomorrow/today + time
        if body and ("tomorrow" in body.lower() or "today" in body.lower()):
            time_bit = _first_time_phrase(summary, body)
            if time_bit and "tomorrow" in body.lower():
                return f"tomorrow {time_bit}"
            if time_bit and "today" in body.lower():
                return f"today {time_bit}"
        return deadline_text
    if _extract_time(deadline_text):
        return deadline_text
    if summary and _extract_time(summary):
        return f"{deadline_text}; {summary}"
    time_bit = _first_time_phrase(summary, body)
    if time_bit:
        return f"{deadline_text} {time_bit}"
    return deadline_text


def normalize_classification(
    result: dict[str, Any],
    *,
    received_at: datetime | str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    """Validate enums, resolve deadlines in IST, and ensure summary fallback."""
    task = result.get("task")
    course = result.get("course_or_topic")
    deadline_text = result.get("deadline_text")
    summary = (result.get("summary") or "").strip()
    category = correct_category_from_text(
        result.get("category"),
        task,
        summary,
        course,
        result.get("subject"),
        body,
    )
    result = {**result, "category": category}
    confidence = _normalize_confidence(result.get("confidence"))
    ref = _reference_now(received_at)
    enriched = _enrich_deadline_text(deadline_text, summary, body=body)
    resolved_date = resolve_deadline(result.get("deadline_date"), enriched, now=ref)

    # Prefer storing the enriched phrase so later Q&A keeps the session time
    if enriched and enriched != deadline_text:
        deadline_text = enriched

    if not summary:
        headline = task or course
        if headline:
            summary = str(headline)
            if deadline_text:
                summary = f"{summary} — due {deadline_text}"

    return {
        "is_actionable": bool(result.get("is_actionable")),
        "is_schedule": bool(result.get("is_schedule")),
        "category": category,
        "item_type": _infer_item_type(result),
        "priority": result.get("priority") or _normalize_priority(result.get("priority")),
        "course_or_topic": course,
        "task": task,
        "deadline_text": deadline_text,
        "deadline_date": resolved_date,
        "sender": result.get("sender"),
        "summary": summary or None,
        "confidence": confidence,
    }


def classify_email(
    subject: str,
    body: str,
    sender: str,
    *,
    received_at: datetime | str | None = None,
    attachment_text: str | None = None,
) -> dict[str, Any] | None:
    ref = _reference_now(received_at)
    today = ref.date().isoformat()
    now_str = ref.strftime("%Y-%m-%d %H:%M %Z")
    system = DEADLINE_PROMPT.format(today=today, now=now_str, today_year=ref.year)
    attach_block = ""
    if attachment_text and attachment_text.strip():
        attach_block = f"\n\nExtracted attachment text:\n{attachment_text[:6000]}"
    user = (
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Email received at (IST reference for relative dates): {now_str}\n\n"
        f"{body}{attach_block}"
    )

    result = call_json(system, user)
    if result is None:
        repair = DEADLINE_REPAIR_PROMPT.format(today=today, now=now_str)
        result = call_json(repair, user)

    if not result or not isinstance(result, dict):
        return None

    result = _boost_category_from_signals(subject, body, sender, result)
    result = _boost_priority_from_signals(subject, body, result)
    combined_body = f"{body}\n{attachment_text or ''}"
    return normalize_classification(result, received_at=ref, body=combined_body)
