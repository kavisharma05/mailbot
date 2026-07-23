"""Shared Telegram formatting contracts.

Two explicit output channels:
- PlainAnswer: LLM Q&A, contact lookup, compose preview — no parse_mode.
- TelegramHtml: /due, digests, alerts, schedule — restricted tags only.

Allowed HTML tags: <b>, <i>, <pre>. All dynamic values must pass through escape_html().
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.timeutil import IST, format_deadline_display, now_ist, parse_iso_deadline

VALID_CATEGORIES = ("deadline", "placement", "certification", "admin", "other")

CATEGORY_LABELS = {
    "deadline": "Deadline",
    "placement": "Placement",
    "certification": "Certification",
    "admin": "Admin",
    "other": "Other",
}

CATEGORY_ICONS = {
    "deadline": "⏰",
    "placement": "💼",
    "certification": "🎓",
    "admin": "📢",
    "other": "📋",
}

PRIORITY_CATEGORIES = frozenset({"placement", "certification"})

ALLOWED_HTML_TAGS = frozenset({"b", "i", "pre"})

DEFAULT_LIST_LIMIT = 20
DIGEST_DUE_LIMIT = 20
DIGEST_OVERDUE_LIMIT = 12
DIGEST_PRIORITY_LIMIT = 12
DIGEST_RECENT_LIMIT = 8

_WHITESPACE_RE = re.compile(r"[ \t]+\n")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def category_icon(category: str) -> str:
    return CATEGORY_ICONS.get(normalize_category(category), "📋")


def escape_html(text: str | None) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def normalize_whitespace(text: str) -> str:
    text = _WHITESPACE_RE.sub("\n", text.strip())
    return _MULTI_BLANK_RE.sub("\n\n", text)


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


def is_overdue_item(item: dict[str, Any], *, tz: ZoneInfo = IST, today: date | None = None) -> bool:
    raw = item.get("deadline_date")
    if not raw:
        return False
    dt = parse_iso_deadline(str(raw))
    if dt is None:
        return False
    local = dt.astimezone(tz)
    ref = today or now_ist().date()
    return local.date() < ref


def sort_assignments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority first, then earliest deadline, then stable task label."""

    def key(item: dict[str, Any]) -> tuple[int, str, str]:
        cat = item.get("category") or "other"
        pri = 0 if is_priority_item(
            cat,
            task=item.get("task"),
            summary=item.get("summary"),
            deadline_date=item.get("deadline_date"),
        ) else 1
        due = item.get("deadline_date") or "9999"
        label = str(item.get("task") or item.get("summary") or "")
        return (pri, due, label.lower())

    return sorted(items, key=key)


def format_section_title(title: str, count: int | None = None) -> str:
    label = escape_html(title)
    if count is not None:
        return f"<b>{label} ({count})</b>"
    return f"<b>{label}</b>"


def format_truncation_footer(shown: int, total: int) -> str:
    remaining = total - shown
    if remaining <= 0:
        return ""
    return f"<i>…and {remaining} more.</i>"


def format_empty_state(title: str, detail: str = "") -> str:
    lines = [format_section_title(title), "", "No open items right now."]
    if detail:
        lines.append(escape_html(detail))
    return "\n".join(lines)


def format_assignment_row(
    item: dict[str, Any],
    *,
    numbered: int | None = None,
    show_category: bool = True,
    overdue: bool = False,
) -> str:
    task = item.get("task") or item.get("summary") or "Untitled"
    course = item.get("course_or_topic") or "—"
    due = format_deadline(item.get("deadline_date"), item.get("deadline_text"))
    cat = normalize_category(item.get("category"))
    icon = category_icon(cat)
    cat_label = CATEGORY_LABELS.get(cat, "Other")

    pri = is_priority_item(
        cat,
        task=task,
        summary=item.get("summary"),
        deadline_date=item.get("deadline_date"),
    )
    pri_mark = "🔴 " if pri else ""
    cat_part = f"[{escape_html(cat_label)}] " if show_category else ""

    if numbered is not None:
        head = f"{numbered}. {pri_mark}{icon} {cat_part}<b>{escape_html(str(task))}</b>"
    elif overdue:
        head = f"⚠️ {pri_mark}<b>{escape_html(str(task))}</b>"
    else:
        head = f"• {pri_mark}{icon} {cat_part}<b>{escape_html(str(task))}</b>"

    lines = [head]
    if overdue:
        lines.append(f"   {icon} [{escape_html(cat_label)}] Course: {escape_html(str(course))}")
    else:
        lines.append(f"   Course: {escape_html(str(course))}")
    lines.append(f"   Due: {escape_html(due)}")
    return "\n".join(lines)


def format_assignment_list(
    items: list[dict[str, Any]],
    *,
    title: str,
    category: str | None = None,
    numbered: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
    overdue: bool = False,
    show_category: bool | None = None,
) -> tuple[str, list[str]]:
    """Return formatted text and the exact item IDs shown (for /done mapping)."""
    sorted_items = sort_assignments(items)
    if not sorted_items:
        return format_empty_state(title), []

    if show_category is None:
        show_category = category is None

    shown = sorted_items[:limit]
    ids = [str(item["id"]) for item in shown if item.get("id")]

    lines = [format_section_title(title, len(sorted_items)), ""]
    for i, item in enumerate(shown, start=1):
        lines.append(
            format_assignment_row(
                item,
                numbered=i if numbered else None,
                show_category=show_category,
                overdue=overdue,
            )
        )
        lines.append("")

    footer = format_truncation_footer(len(shown), len(sorted_items))
    if footer:
        lines.append(footer)
    elif len(sorted_items) > limit:
        lines.append(f"<i>Showing {limit} of {len(sorted_items)} open items.</i>")

    return normalize_whitespace("\n".join(lines)), ids


def format_digest_section(
    title: str,
    items: list[dict[str, Any]],
    *,
    overdue: bool = False,
    max_shown: int | None = None,
    exclude_ids: set[str] | None = None,
) -> str:
    exclude_ids = exclude_ids or set()
    filtered = [item for item in items if str(item.get("id") or "") not in exclude_ids]
    if not filtered:
        return ""

    limit = max_shown or len(filtered)
    shown = filtered[:limit]
    lines = [format_section_title(title, len(filtered))]
    for item in shown:
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        due = format_deadline(item.get("deadline_date"), item.get("deadline_text"))
        cat = normalize_category(item.get("category"))
        icon = category_icon(cat)
        cat_label = CATEGORY_LABELS.get(cat, "Other")
        bullet = "⚠️ " if overdue else "• "
        lines.append(f"{bullet}<b>{escape_html(str(task))}</b>")
        lines.append(
            f"   {icon} [{escape_html(cat_label)}] Course: {escape_html(str(course))}"
        )
        lines.append(f"   Due: {escape_html(due)}")
        lines.append("")

    footer = format_truncation_footer(len(shown), len(filtered))
    if footer:
        lines.append(footer)
    return normalize_whitespace("\n".join(lines))


def collect_priority_items(*lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for items in lists:
        for item in items:
            iid = item.get("id")
            if iid and iid in seen:
                continue
            if iid:
                seen.add(str(iid))
            if is_priority_item(
                item.get("category") or "other",
                task=item.get("task"),
                summary=item.get("summary"),
                deadline_date=item.get("deadline_date"),
            ):
                out.append(item)
    return sort_assignments(out)


def format_due_list(items: list[dict[str, Any]], category: str | None) -> tuple[str, list[str]]:
    title = CATEGORY_LABELS.get(category or "", "Open items")
    return format_assignment_list(
        items,
        title=title,
        category=category,
        numbered=True,
        limit=DEFAULT_LIST_LIMIT,
        show_category=category is None,
    )


def format_morning_digest(
    *,
    today: datetime,
    classes_text: str,
    today_items: list[dict[str, Any]],
    recent_items: list[dict[str, Any]],
) -> str:
    date_str = today.strftime("%a, %d %b %Y")
    priority = collect_priority_items(today_items, recent_items)
    priority_ids = {str(i.get("id")) for i in priority if i.get("id")}

    lines = [
        "<b>☀️ Good morning — your day</b>",
        f"<i>{escape_html(date_str)} (IST)</i>",
        "",
    ]

    if priority:
        section = format_digest_section(
            "🔴 Priority",
            priority,
            max_shown=DIGEST_PRIORITY_LIMIT,
        )
        if section:
            lines.extend([section, ""])

    if classes_text and "📚" in classes_text:
        lines.extend([classes_text.strip(), ""])
    elif not today_items:
        lines.append("<i>📚 Classes unavailable — run /classes to refresh.</i>")
        lines.append("")

    due_section = format_digest_section(
        "Due today",
        today_items,
        max_shown=DIGEST_DUE_LIMIT,
        exclude_ids=priority_ids,
    )
    if due_section:
        lines.extend([due_section, ""])
    else:
        lines.extend(["<b>Due today</b>", "", "Nothing open due today.", ""])

    recent_only = [
        item
        for item in recent_items
        if str(item.get("id") or "") not in priority_ids
        and str(item.get("id") or "") not in {str(i.get("id")) for i in today_items}
    ]
    if recent_only:
        lines.append(format_section_title(f"Recently tracked", len(recent_only)))
        for item in recent_only[:DIGEST_RECENT_LIMIT]:
            task = item.get("task") or item.get("summary") or "Untitled"
            due = format_deadline(item.get("deadline_date"), item.get("deadline_text"))
            cat = normalize_category(item.get("category"))
            icon = category_icon(cat)
            cat_label = CATEGORY_LABELS.get(cat, "Other")
            lines.append(f"• <b>{escape_html(str(task))}</b> {icon} [{escape_html(cat_label)}]")
            lines.append(f"   Due: {escape_html(due)}")
            lines.append("")
        footer = format_truncation_footer(min(len(recent_only), DIGEST_RECENT_LIMIT), len(recent_only))
        if footer:
            lines.append(footer)

    return normalize_whitespace("\n".join(lines))


def format_evening_digest(
    *,
    tomorrow: datetime,
    classes_text: str,
    overdue_items: list[dict[str, Any]],
    tomorrow_items: list[dict[str, Any]],
) -> str:
    date_str = tomorrow.strftime("%a, %d %b %Y")
    priority = collect_priority_items(overdue_items, tomorrow_items)
    priority_ids = {str(i.get("id")) for i in priority if i.get("id")}

    lines = [
        "<b>🌙 Evening — tomorrow</b>",
        f"<i>{escape_html(date_str)} (IST)</i>",
        "",
    ]

    if priority:
        section = format_digest_section(
            "🔴 Priority",
            priority,
            max_shown=DIGEST_PRIORITY_LIMIT,
        )
        if section:
            lines.extend([section, ""])

    overdue_section = format_digest_section(
        "Overdue",
        overdue_items,
        overdue=True,
        max_shown=DIGEST_OVERDUE_LIMIT,
        exclude_ids=priority_ids,
    )
    if overdue_section:
        lines.extend([overdue_section, ""])

    if classes_text and "📚" in classes_text:
        lines.extend([classes_text.strip(), ""])

    tomorrow_section = format_digest_section(
        "Due tomorrow",
        tomorrow_items,
        max_shown=DIGEST_DUE_LIMIT,
        exclude_ids=priority_ids,
    )
    if tomorrow_section:
        lines.append(tomorrow_section)
    elif not classes_text:
        lines.append("Nothing due tomorrow and no classes scheduled yet.")

    return normalize_whitespace("\n".join(lines))
