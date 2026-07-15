import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ai.client import call_json, call_text, get_last_groq_error
from ai.prompts import ASSISTANT_PROMPT, GMAIL_QUERY_PROMPT
from gmail_api.client import get_message_details, search_message_ids

logger = logging.getLogger(__name__)

# Keep prompts small to reduce Groq free-tier token burn
_MAX_EMAILS = 5
_BODY_CHARS = 900
_MAX_USER_CHARS = 6000
_PASTED_CHARS = 1800
_MAX_ITEMS = 20

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "for", "on", "in", "at", "to",
    "of", "and", "or", "me", "my", "all", "give", "tell", "what", "when", "why",
    "how", "any", "about", "also", "date", "mail", "email", "received", "deadline",
    "deadlines", "tomorrow", "today", "see", "there", "this", "that", "with",
    "from", "did", "you", "miss", "reading", "task", "class", "july", "imt",
}


def _looks_like_pasted_email(text: str) -> bool:
    lower = text.lower()
    return len(text) > 180 and (
        "dear " in lower
        or "regards" in lower
        or "professor" in lower
        or "students," in lower
    )


def _extract_keywords(text: str, max_words: int = 6) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
    picked: list[str] = []
    for word in words:
        if word.lower() in _STOPWORDS:
            continue
        if word not in picked:
            picked.append(word)
        if len(picked) >= max_words:
            break
    return picked


def _queries_from_pasted_email(text: str) -> list[str]:
    queries: list[str] = []
    for match in re.finditer(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", text):
        name = match.group(1)
        if any(w in name.lower() for w in ("professor", "chairperson", "dear", "thanks")):
            continue
        if len(name.split()) >= 2:
            queries.append(f'from:"{name}" newer_than:90d')
            break
    for phrase in ("Brady Training", "Organisational Behaviour", "Organizational Behaviour"):
        if phrase.lower() in text.lower():
            queries.append(f'"{phrase}" newer_than:90d')
    keywords = _extract_keywords(text, max_words=4)
    if keywords:
        queries.append(f"{' '.join(keywords)} newer_than:90d")
    return queries


def _is_simple_deadline_question(question: str) -> bool:
    q = question.lower()
    return any(
        w in q
        for w in (
            "deadline",
            "due",
            "tomorrow",
            "today",
            "what's due",
            "what is due",
            "any deadline",
        )
    )


def build_gmail_queries(
    question: str,
    pasted_email: str | None = None,
    *,
    use_llm: bool = True,
) -> list[str]:
    queries: list[str] = []

    # Skip LLM query builder for simple deadline questions to save tokens
    if use_llm and not _is_simple_deadline_question(question):
        today = datetime.now().strftime("%Y-%m-%d")
        system = GMAIL_QUERY_PROMPT.format(today=today)
        result = call_json(system, f"Question:\n{question}")
        if result:
            if isinstance(result.get("queries"), list):
                queries.extend(
                    q.strip() for q in result["queries"] if isinstance(q, str) and q.strip()
                )
            elif isinstance(result.get("query"), str) and result["query"].strip():
                queries.append(result["query"].strip())

    keywords = _extract_keywords(question)
    if keywords:
        queries.append(f"({' OR '.join(keywords)}) newer_than:30d")

    if pasted_email:
        queries.extend(_queries_from_pasted_email(pasted_email))

    if not queries:
        queries.append("newer_than:14d")

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique[:3]


def _fetch_relevant_emails(question: str, pasted_email: str | None = None) -> list[dict[str, Any]]:
    queries = build_gmail_queries(question, pasted_email=pasted_email)
    seen_ids: set[str] = set()
    emails: list[dict[str, Any]] = []

    for query in queries:
        if len(emails) >= _MAX_EMAILS:
            break
        try:
            message_ids = search_message_ids(query, max_results=_MAX_EMAILS)
        except Exception as exc:
            logger.warning("Gmail search failed for %r: %s", query, exc)
            continue

        for message_id in message_ids:
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            try:
                details = get_message_details(message_id)
            except Exception as exc:
                logger.warning("Fetching message %s failed: %s", message_id, exc)
                continue
            emails.append(
                {
                    "subject": details.get("subject"),
                    "sender": details.get("sender"),
                    "received_at": details.get("received_at"),
                    "body": (details.get("body") or "")[:_BODY_CHARS],
                }
            )
            if len(emails) >= _MAX_EMAILS:
                break

    return emails


def _parse_target_date(question: str) -> datetime | None:
    """Resolve 'tomorrow' / 'today' / explicit dates in the question to a local date."""
    q = question.lower()
    now = datetime.now().astimezone()
    if "tomorrow" in q:
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in q:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan|january|feb|february|mar|march|apr|april|"
        r"may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|"
        r"nov|november|dec|december)\s+(\d{4})",
        q,
    )
    if m:
        months = {
            "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
            "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
            "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
        }
        day, mon, year = int(m.group(1)), months[m.group(2)], int(m.group(3))
        return datetime(year, mon, day, tzinfo=now.tzinfo)

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=now.tzinfo)
    return None


def _item_deadline_dt(item: dict[str, Any]) -> datetime | None:
    value = item.get("deadline_date")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except ValueError:
        return None


def _deterministic_deadline_answer(question: str, items: list[dict[str, Any]]) -> str | None:
    """Answer deadline questions from DB without Groq when AI is unavailable."""
    if not _is_simple_deadline_question(question) and _parse_target_date(question) is None:
        return None

    target = _parse_target_date(question)
    if target is None and "deadline" not in question.lower() and "due" not in question.lower():
        return None

    matched: list[dict[str, Any]] = []
    for item in items:
        if item.get("status") and item.get("status") != "open":
            continue
        dt = _item_deadline_dt(item)
        if target is not None:
            if dt is None:
                text = (item.get("deadline_text") or "").lower()
                if target.date() == (datetime.now().astimezone() + timedelta(days=1)).date():
                    if "tomorrow" not in text:
                        continue
                elif "today" not in text:
                    continue
            elif dt.date() != target.date():
                continue
        matched.append(item)

    label = target.strftime("%d %b %Y") if target else "upcoming"
    if not matched:
        return f"No open deadlines found for {label} in tracked items."

    lines = [f"Deadlines for {label} ({len(matched)}):", ""]
    for i, item in enumerate(matched[:15], start=1):
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        dt = _item_deadline_dt(item)
        due = dt.strftime("%d %b %Y %I:%M %p") if dt else (item.get("deadline_text") or "Not specified")
        created = item.get("created_at") or "unknown"
        lines.append(f"{i}. {task}")
        lines.append(f"   Course: {course}")
        lines.append(f"   Due: {due}")
        lines.append(f"   Tracked since: {created}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_user_prompt(
    question: str,
    items: list[dict[str, Any]],
    emails: list[dict[str, Any]],
    category: str | None,
    pasted_email: str | None,
) -> str:
    scope = f"Category hint: {category}" if category else "Category hint: none"
    slim_items = [
        {
            "category": i.get("category"),
            "course_or_topic": i.get("course_or_topic"),
            "task": i.get("task"),
            "deadline_text": i.get("deadline_text"),
            "deadline_date": i.get("deadline_date"),
            "summary": i.get("summary"),
            "status": i.get("status"),
        }
        for i in items[:_MAX_ITEMS]
    ]
    parts = [scope, ""]

    if pasted_email:
        parts.append("Student pasted this email text:")
        parts.append(pasted_email[:_PASTED_CHARS])
        parts.append("")

    parts.append(f"Tracked items (JSON):\n{json.dumps(slim_items, default=str)}")
    parts.append(f"\nMatching Gmail emails (JSON):\n{json.dumps(emails, default=str)}")
    parts.append(f"\nStudent question:\n{question}")

    text = "\n".join(parts)
    if len(text) > _MAX_USER_CHARS:
        text = text[:_MAX_USER_CHARS] + "\n...[truncated]"
    return text


def answer_question(
    question: str,
    items: list[dict[str, Any]],
    category: str | None = None,
    *,
    scan_gmail: bool = True,
    pasted_email: str | None = None,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    system = ASSISTANT_PROMPT.format(today=today)

    if _is_simple_deadline_question(question) or _parse_target_date(question):
        deterministic = _deterministic_deadline_answer(question, items)
        if deterministic and not pasted_email:
            emails = _fetch_relevant_emails(question, pasted_email=pasted_email) if scan_gmail else []
            user = _build_user_prompt(question, items, emails, category, pasted_email)
            answer = call_text(system, user)
            if answer:
                return answer
            return deterministic

    emails = _fetch_relevant_emails(question, pasted_email=pasted_email) if scan_gmail else []
    user = _build_user_prompt(question, items, emails, category, pasted_email)

    answer = call_text(system, user)
    if answer:
        return answer

    _, status = get_last_groq_error()
    deterministic = _deterministic_deadline_answer(question, items)
    if deterministic:
        if status == 429:
            return (
                deterministic
                + "\n\n(Note: AI is temporarily rate-limited; this list is from tracked items only.)"
            )
        return deterministic

    if status == 429:
        return (
            "Groq AI rate limit hit for today (token quota). "
            "Try again later, or use /due to see tracked deadlines without AI."
        )

    if pasted_email:
        return (
            "I found the email you pasted but couldn't finish the full analysis. "
            "From what you shared: it mentions tasks for tomorrow's session — "
            "check the Brady Training Program case and values chapter for OB."
        )
    return "I couldn't generate an answer right now. Try /due, or ask again in a few minutes."


def split_question_and_paste(text: str) -> tuple[str, str | None]:
    """If the message is mostly a pasted email, split question vs paste."""
    if not _looks_like_pasted_email(text):
        return text, None

    lines = text.strip().splitlines()
    question_lines: list[str] = []
    paste_start = 0
    for i, line in enumerate(lines):
        if len(line) < 120 and not _looks_like_pasted_email(line):
            question_lines.append(line)
            paste_start = i + 1
        else:
            break

    pasted = "\n".join(lines[paste_start:]).strip()
    question = " ".join(question_lines).strip() or "What does this email mean for my deadlines?"

    if not pasted:
        return text, None
    return question, pasted
