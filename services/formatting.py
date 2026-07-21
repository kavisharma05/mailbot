"""Shared HTML formatting and category labels for Telegram alerts."""

from __future__ import annotations

from services.timeutil import IST, format_deadline_display

VALID_CATEGORIES = ("deadline", "placement", "certification", "admin", "other")

CATEGORY_LABELS = {
    "deadline": "Deadline",
    "placement": "Placement",
    "certification": "Certification",
    "admin": "Admin",
    "other": "Other",
}

PRIORITY_CATEGORIES = frozenset({"deadline", "placement", "certification"})


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
) -> bool:
    if is_priority_category(category):
        return True
    return category == "deadline" and is_reading_task(task, summary)


def priority_prefix(category: str, *, task: str | None = None, summary: str | None = None) -> str:
    if is_priority_item(category, task=task, summary=summary):
        return "🔴 <b>IMPORTANT</b>\n"
    return ""
