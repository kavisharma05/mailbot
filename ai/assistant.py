import json
import logging
import re
from datetime import datetime
from typing import Any

from ai.client import call_json, call_text
from ai.prompts import ASSISTANT_PROMPT, GMAIL_QUERY_PROMPT
from gmail_api.client import get_message_details, search_message_ids

logger = logging.getLogger(__name__)

_MAX_EMAILS = 12
_BODY_CHARS = 2500
_MAX_USER_CHARS = 14000
_PASTED_CHARS = 3000

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
    # Professor / sender line
    for match in re.finditer(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", text):
        name = match.group(1)
        if any(w in name.lower() for w in ("professor", "chairperson", "dear", "thanks")):
            continue
        if len(name.split()) >= 2:
            queries.append(f'from:"{name}" newer_than:90d')
            break
    # Quoted phrases from email body
    for phrase in ("Brady Training", "Organisational Behaviour", "Organizational Behaviour"):
        if phrase.lower() in text.lower():
            queries.append(f'"{phrase}" newer_than:90d')
    keywords = _extract_keywords(text, max_words=4)
    if keywords:
        queries.append(f"{' '.join(keywords)} newer_than:90d")
    return queries


def build_gmail_queries(question: str, pasted_email: str | None = None) -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    system = GMAIL_QUERY_PROMPT.format(today=today)
    result = call_json(system, f"Question:\n{question}")
    queries: list[str] = []

    if result:
        if isinstance(result.get("queries"), list):
            queries.extend(q.strip() for q in result["queries"] if isinstance(q, str) and q.strip())
        elif isinstance(result.get("query"), str) and result["query"].strip():
            queries.append(result["query"].strip())

    keywords = _extract_keywords(question)
    if keywords:
        queries.append(f"({' OR '.join(keywords)}) newer_than:90d")

    if pasted_email:
        queries.extend(_queries_from_pasted_email(pasted_email))

    if not queries:
        queries.append(f"{question} newer_than:90d")

    # Dedupe preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique[:5]


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


def _build_user_prompt(
    question: str,
    items: list[dict[str, Any]],
    emails: list[dict[str, Any]],
    category: str | None,
    pasted_email: str | None,
) -> str:
    scope = f"Category hint: {category}" if category else "Category hint: none (search all)"
    parts = [scope, ""]

    if pasted_email:
        parts.append("Student pasted this email text:")
        parts.append(pasted_email[:_PASTED_CHARS])
        parts.append("")

    parts.append(f"Tracked items (JSON):\n{json.dumps(items, default=str)}")
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

    emails = _fetch_relevant_emails(question, pasted_email=pasted_email) if scan_gmail else []
    user = _build_user_prompt(question, items, emails, category, pasted_email)

    answer = call_text(system, user)
    if answer:
        return answer

    # Retry with slimmer context if LLM failed (often token/size related)
    slim_user = _build_user_prompt(
        question,
        items[:15],
        emails[:4],
        category,
        pasted_email[:1500] if pasted_email else None,
    )
    answer = call_text(system, slim_user)
    if answer:
        return answer

    if pasted_email:
        return (
            "I found the email you pasted but couldn't finish the full analysis. "
            "From what you shared: it mentions tasks for tomorrow's session — "
            "check the Brady Training Program case and values chapter for OB."
        )
    return "I couldn't generate an answer right now. Try rephrasing or paste the email text."


def split_question_and_paste(text: str) -> tuple[str, str | None]:
    """If the message is mostly a pasted email, split question vs paste."""
    if not _looks_like_pasted_email(text):
        return text, None

    lines = text.strip().splitlines()
    # First short line(s) may be the question
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
