"""Shared HTML formatting and category labels for Telegram alerts."""

from __future__ import annotations

from datetime import timedelta

from services.timeutil import IST, format_deadline_display, now_ist, parse_iso_deadline

VALID_CATEGORIES = ("deadline", "placement", "certification", "admin", "other")

CATEGORY_LABELS = {
    "deadline": "Deadline",
    "placement": "Placement",
    "certification": "Certification",
    "admin": "Admin",
    "other": "Other",
}

PRIORITY_CATEGORIES = frozenset({"placement", "certification"})


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize_category(value: str | None) -> str:
    if value in VALID_CATEGORIES:
        return value
    return "other"


def format_deadline(deadline_date: str | None, deadline_text: str | None) -> str:
    return format_deadline_display(deadline_date, deadline_text, tz=IST)


def is_priority_category(category: str) -> bool:
    return category in PRIORITY_CATEGORIES


def is_reading_task(task: str | None, summary: str | None = None) -> bool:
    blob = f"{task or ''} {summary or ''}".lower()
    return any(
        w in blob
        for w in (
            "read",
            "reading",
            "chapter",
            "case study",
            "case",
            "prepare",
            "prep for",
            "book",
            "article",
        )
    )


def is_priority_item(
    category: str,
    *,
    task: str | None = None,
    summary: str | None = None,
    deadline_date: str | None = None,
) -> bool:
    if category in ("placement", "certification"):
        return True
    if category == "deadline" and deadline_date:
        dt = parse_iso_deadline(deadline_date)
        if dt is not None:
            delta = dt - now_ist()
            if timedelta(0) <= delta <= timedelta(hours=48):
                return True
    return False


def priority_prefix(
    category: str,
    *,
    task: str | None = None,
    summary: str | None = None,
    deadline_date: str | None = None,
) -> str:
    if is_priority_item(category, task=task, summary=summary, deadline_date=deadline_date):
        return "🔴 <b>IMPORTANT</b>\n"
    return ""
