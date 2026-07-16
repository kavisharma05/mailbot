from typing import Any

from ai.client import call_json
from ai.prompts import DEADLINE_PROMPT, DEADLINE_REPAIR_PROMPT
from services.timeutil import now_ist, resolve_deadline, today_ist

VALID_CATEGORIES = ("deadline", "placement", "admin", "other")
VALID_CONFIDENCE = ("high", "medium", "low")


def _normalize_category(value: str | None) -> str:
    if value in VALID_CATEGORIES:
        return value
    return "other"


def _normalize_confidence(value: str | None) -> str:
    if value in VALID_CONFIDENCE:
        return value
    return "low"


def normalize_classification(result: dict[str, Any]) -> dict[str, Any]:
    """Validate enums, resolve deadlines in IST, and ensure summary fallback."""
    category = _normalize_category(result.get("category"))
    confidence = _normalize_confidence(result.get("confidence"))
    task = result.get("task")
    course = result.get("course_or_topic")
    deadline_text = result.get("deadline_text")
    resolved_date = resolve_deadline(result.get("deadline_date"), deadline_text, now=now_ist())

    summary = (result.get("summary") or "").strip()
    if not summary:
        headline = task or course
        if headline:
            summary = str(headline)
            if deadline_text:
                summary = f"{summary} — due {deadline_text}"

    return {
        "is_actionable": bool(result.get("is_actionable")),
        "category": category,
        "course_or_topic": course,
        "task": task,
        "deadline_text": deadline_text,
        "deadline_date": resolved_date,
        "sender": result.get("sender"),
        "summary": summary or None,
        "confidence": confidence,
    }


def classify_email(subject: str, body: str, sender: str) -> dict[str, Any] | None:
    ref = now_ist()
    today = today_ist().isoformat()
    now_str = ref.strftime("%Y-%m-%d %H:%M %Z")
    system = DEADLINE_PROMPT.format(today=today, now=now_str, today_year=ref.year)
    user = f"From: {sender}\nSubject: {subject}\n\n{body}"

    result = call_json(system, user)
    if result is None:
        repair = DEADLINE_REPAIR_PROMPT.format(today=today, now=now_str)
        result = call_json(repair, user)

    if not result or not isinstance(result, dict):
        return None

    return normalize_classification(result)
