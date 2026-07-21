import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from ai.client import call_json, call_text, get_last_groq_error
from ai.prompts import ASSISTANT_PROMPT, GMAIL_QUERY_PROMPT, VERIFIED_ASSISTANT_PROMPT
from config import get_settings
from gmail_api.client import (
    count_messages,
    get_message_details,
    list_message_summaries,
    search_message_ids,
)
from services.timeutil import (
    IST,
    format_deadline_display,
    item_effective_deadline_dt,
    now_ist,
    resolve_deadline_text,
    today_ist,
)

logger = logging.getLogger(__name__)

_MAX_EMAILS = 8
_MAX_MAILBOX_EMAILS = 25
_BODY_CHARS = 1500
_MAILBOX_BODY_CHARS = 1800
_ATTACHMENT_CHARS = 6000
_ATTACHMENT_CHARS_PER_EMAIL = 3500
_MAX_ATTACHMENT_EMAILS = 3
_MAX_USER_CHARS = 14000
_PASTED_CHARS = 1800
_MAX_ITEMS = 20

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "for", "on", "in", "at", "to",
    "of", "and", "or", "me", "my", "all", "give", "tell", "what", "when", "why",
    "how", "any", "about", "also", "date", "mail", "email", "emails", "mails",
    "received", "deadline", "deadlines", "tomorrow", "today", "see", "there",
    "this", "that", "with", "from", "did", "you", "miss", "reading", "task",
    "class", "july", "imt", "many", "total", "count", "whom", "got", "have",
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


def _asks_undated_tasks(question: str) -> bool:
    q = question.lower()
    return any(
        phrase in q
        for phrase in (
            "no specific deadline",
            "no deadline",
            "without deadline",
            "without a deadline",
            "unspecified deadline",
            "missing deadline",
            "no due date",
            "without due",
            "tasks with no",
            "no specific due",
        )
    )


_PERSON_STOP = {
    "today",
    "yesterday",
    "mail",
    "email",
    "emails",
    "mails",
    "the",
    "a",
    "an",
    "whom",
    "who",
    "what",
    "which",
    "where",
    "when",
    "how",
    "many",
    "all",
    "anyone",
    "someone",
    "professor",
    "mam",
    "sir",
    "ma'am",
    "maam",
}


def _is_mailbox_question(question: str) -> bool:
    """General Gmail questions: counts, senders, content — not pure deadline refreshes."""
    q = question.lower().strip()
    if _asks_undated_tasks(q):
        return False
    if _is_due_list_question(q) and not any(w in q for w in ("mail", "email", "inbox")):
        return False
    mailish = any(w in q for w in ("mail", "email", "inbox", "sender", "received"))
    intent = any(
        w in q
        for w in (
            "how many",
            "count",
            "total",
            "from whom",
            "who sent",
            "who all",
            "list",
            "summar",
            "what did",
            "said",
            "about",
            "content",
            "subject",
            "unread",
            "got today",
            "received today",
            "mails today",
            "emails today",
            "mails from",
            "emails from",
            "from ",
        )
    )
    if mailish and intent:
        return True
    if any(
        p in q
        for p in (
            "how many mail",
            "how many email",
            "mails i got",
            "emails i got",
            "from whom",
            "mails from",
            "emails from",
            "total mails",
            "total emails",
        )
    ):
        return True
    # "what did Saleena say about Brady" / "Saleena Khan email"
    if re.search(r"\bwhat did\b.+\b(say|write|mention)\b", q):
        return True
    if re.search(r"\b(from|by)\s+[a-z]", q):
        return True
    if _extract_person_query(question) and any(
        w in q for w in ("say", "said", "about", "mail", "email", "wrote", "sent")
    ):
        return True
    return False


def _extract_person_query(question: str) -> str | None:
    """Pull a person/email fragment for Gmail from: search."""
    q = question.strip()
    m = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", q)
    if m:
        return m.group(1)

    m = re.search(
        r"(?:from|by)\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.']*){0,3})",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip(" .,?!")
        parts = [p for p in name.split() if p.lower() not in _PERSON_STOP]
        if parts:
            return " ".join(parts)

    # Casual professor forms: "shanthi mam", "marks from santhi mam", "in shanthi mam's class"
    m = re.search(
        r"\b([A-Za-z][A-Za-z.'-]+)\s+(?:mam|ma'am|maam|sir)\b",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    m = re.search(
        r"\b(?:in|from|for)\s+([A-Za-z][A-Za-z.'-]+)(?:'s)?\s+(?:class|course|subject|mail|email)\b",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # "what did Saleena Khan say" / "Saleena's mail"
    m = re.search(
        r"(?:what did|did)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+(?:say|write|mention|send)",
        q,
    )
    if m:
        return m.group(1).strip()

    m = re.search(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)(?:'s)?\s+(?:mail|email|message)",
        q,
    )
    if m:
        return m.group(1).strip()

    return None


def _wants_attachment_text(question: str) -> bool:
    """True when the answer likely lives in PDF/Excel attachments (marks, results, etc.)."""
    q = question.lower()
    return any(
        w in q
        for w in (
            "mark",
            "marks",
            "marksheet",
            "grade",
            "grades",
            "score",
            "scores",
            "result",
            "results",
            "cgpa",
            "sgpa",
            "gpa",
            "percentile",
            "rank",
            "attachment",
            "attached",
            "pdf",
            "excel",
            "sheet",
            "what did i get",
            "what i got",
            "my score",
            "my grade",
            "my marks",
            "my result",
        )
    )


def _gmail_day_bounds_query(day: datetime | None = None) -> str:
    """Gmail after:/before: window for an IST calendar day."""
    d = (day or now_ist()).astimezone(IST).date()
    nxt = d + timedelta(days=1)
    return f"after:{d.year}/{d.month:02d}/{d.day:02d} before:{nxt.year}/{nxt.month:02d}/{nxt.day:02d}"


def _build_mailbox_queries(question: str) -> list[str]:
    q = question.lower()
    person = _extract_person_query(question)
    queries: list[str] = []

    if "today" in q or "got today" in q or "received today" in q:
        day_q = _gmail_day_bounds_query()
        if person:
            queries.append(f'from:("{person}") {day_q}')
            queries.append(f"from:({person}) {day_q}")
        queries.append(day_q)
        queries.append(f"(in:inbox OR in:spam) {day_q}")
    elif "yesterday" in q:
        day_q = _gmail_day_bounds_query(now_ist() - timedelta(days=1))
        if person:
            queries.append(f'from:("{person}") {day_q}')
        queries.append(day_q)
    elif person:
        queries.append(f'from:("{person}") newer_than:90d')
        queries.append(f"from:({person.split()[-1]}) newer_than:90d")
        keywords = _extract_keywords(question, max_words=4)
        if keywords:
            queries.append(f'from:("{person}") {" ".join(keywords)} newer_than:90d')
    else:
        keywords = _extract_keywords(question, max_words=5)
        if keywords:
            queries.append(f"{' '.join(keywords)} newer_than:30d")
            queries.append(f"({' OR '.join(keywords)}) newer_than:60d")
        # LLM query builder only if heuristics are empty (saves free-tier tokens)
        if not keywords and _should_use_llm_queries(question):
            today = today_ist().isoformat()
            system = GMAIL_QUERY_PROMPT.format(today=today)
            result = call_json(system, f"Question:\n{question}")
            if result and isinstance(result.get("queries"), list):
                queries.extend(
                    x.strip() for x in result["queries"] if isinstance(x, str) and x.strip()
                )

    if not queries:
        queries.append(_gmail_day_bounds_query())

    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for item in queries:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out[:4]


def _wants_mail_bodies(question: str) -> bool:
    q = question.lower()
    return _wants_attachment_text(question) or any(
        w in q
        for w in (
            "said",
            "say",
            "content",
            "about",
            "summar",
            "what did",
            "meaning",
            "detail",
            "body",
            "wrote",
            "mention",
            "reading",
            "case",
            "attachment",
        )
    )


def _email_for_prompt(
    details: dict[str, Any],
    *,
    body_chars: int = _BODY_CHARS,
    attachment_chars: int = _ATTACHMENT_CHARS,
    include_attachments: bool = True,
) -> dict[str, Any]:
    """Build a slim email dict for LLM context, optionally including attachment text."""
    item: dict[str, Any] = {
        "message_id": details.get("message_id"),
        "subject": details.get("subject"),
        "sender": details.get("sender"),
        "received_at": details.get("received_at"),
        "body": (details.get("body") or "")[:body_chars],
    }
    if include_attachments:
        attachments = details.get("attachments") or []
        if attachments:
            item["attachment_filenames"] = [
                a.get("filename") for a in attachments if a.get("filename")
            ]
        attach_text = (details.get("attachment_text") or "").strip()
        if attach_text:
            item["attachment_text"] = attach_text[:attachment_chars]
    return item


def _apply_attachment_budget(
    emails: list[dict[str, Any]],
    *,
    wants_attachments: bool,
) -> list[dict[str, Any]]:
    """Keep attachment text on top-N emails only; cap chars per email."""
    if not wants_attachments or not emails:
        return emails

    out: list[dict[str, Any]] = []
    for i, mail in enumerate(emails):
        m = dict(mail)
        if i < _MAX_ATTACHMENT_EMAILS:
            mid = m.get("message_id")
            attach_text = (m.get("attachment_text") or "").strip()
            if mid and not attach_text:
                try:
                    details = get_message_details(mid, load_attachments=True)
                    enriched = _email_for_prompt(
                        details,
                        body_chars=max(len(m.get("body") or ""), _BODY_CHARS),
                        attachment_chars=_ATTACHMENT_CHARS_PER_EMAIL,
                        include_attachments=True,
                    )
                    if m.get("body"):
                        enriched["body"] = m["body"]
                    m.update(enriched)
                except Exception as exc:
                    logger.warning("Attachment enrich failed for %s: %s", mid, exc)
            elif attach_text:
                m["attachment_text"] = attach_text[:_ATTACHMENT_CHARS_PER_EMAIL]
        else:
            m.pop("attachment_text", None)
            m.pop("attachment_filenames", None)
        out.append(m)
    return out


def _gather_mailbox_context(
    question: str,
    *,
    force_bodies: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Stats + message list for mailbox Q&A."""
    queries = _build_mailbox_queries(question)
    primary = queries[0]
    wants_bodies = force_bodies or _wants_mail_bodies(question) or "whom" in question.lower() or "who" in question.lower()
    wants_attachments = force_bodies or _wants_attachment_text(question)

    try:
        stats = count_messages(primary, hard_cap=300)
    except Exception as exc:
        logger.warning("count_messages failed for %r: %s", primary, exc)
        stats = {"query": primary, "count": 0, "capped": False, "error": str(exc)}

    emails: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        try:
            batch = list_message_summaries(
                query,
                max_results=_MAX_MAILBOX_EMAILS,
                include_body=force_bodies or wants_bodies or "how many" not in question.lower(),
                body_chars=_MAILBOX_BODY_CHARS if wants_bodies or force_bodies else 400,
                load_attachments=False,
                attachment_chars=_ATTACHMENT_CHARS_PER_EMAIL,
            )
        except Exception as exc:
            logger.warning("list_message_summaries failed for %r: %s", query, exc)
            continue
        for mail in batch:
            mid = mail.get("message_id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            emails.append(mail)
            if len(emails) >= _MAX_MAILBOX_EMAILS:
                break
        if len(emails) >= _MAX_MAILBOX_EMAILS:
            break

    # Sender breakdown for "from whom" / how many today
    senders: dict[str, int] = {}
    for mail in emails:
        sender = (mail.get("sender") or "unknown").strip()
        senders[sender] = senders.get(sender, 0) + 1

    stats = {
        **stats,
        "queries_tried": queries,
        "listed": len(emails),
        "senders": [
            {"sender": s, "count": c}
            for s, c in sorted(senders.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "person_filter": _extract_person_query(question),
    }
    if wants_attachments:
        emails = _apply_attachment_budget(emails, wants_attachments=True)
    return stats, emails


def _format_mailbox_fallback(stats: dict[str, Any], emails: list[dict[str, Any]], question: str) -> str:
    """Deterministic mailbox answer when LLM is down."""
    person = stats.get("person_filter")
    count = stats.get("count", 0)
    capped = stats.get("capped")
    lines = []
    if person:
        lines.append(f"Mails from {person}: {count}" + ("+ (count capped)" if capped else ""))
    else:
        lines.append(f"Matching mails: {count}" + ("+ (count capped)" if capped else ""))
    lines.append(f"Query: {stats.get('query')}")
    lines.append("")
    if stats.get("senders"):
        lines.append("From whom (in listed sample):")
        for row in stats["senders"][:15]:
            lines.append(f"• {row['sender']} — {row['count']}")
        lines.append("")
    if emails:
        lines.append(f"Recent messages ({min(len(emails), 15)}):")
        for i, mail in enumerate(emails[:15], start=1):
            lines.append(f"{i}. {mail.get('subject') or '(no subject)'}")
            lines.append(f"   From: {mail.get('sender') or 'unknown'}")
            lines.append(f"   Received: {mail.get('received_at') or 'unknown'}")
            if mail.get("body") and _wants_mail_bodies(question):
                snippet = re.sub(r"\s+", " ", str(mail["body"]))[:220]
                lines.append(f"   Snippet: {snippet}")
            lines.append("")
    lines.append("(Offline format — free AI quota resting; data is live from Gmail.)")
    return "\n".join(lines).strip()


def _format_sources_block(emails: list[dict[str, Any]], *, limit: int = 5) -> str:
    """Deterministic evidence footer for verified mode."""
    if not emails:
        return ""

    lines = ["", "Sources (from Gmail):", ""]
    for i, mail in enumerate(emails[:limit], start=1):
        subject = mail.get("subject") or "(no subject)"
        sender = mail.get("sender") or "unknown"
        received = mail.get("received_at") or "unknown"
        lines.append(f"{i}. {subject}")
        lines.append(f"   From: {sender}")
        lines.append(f"   Received: {received}")
        body = mail.get("body")
        if body:
            snippet = re.sub(r"\s+", " ", str(body))[:200]
            lines.append(f"   Snippet: {snippet}")
        lines.append("")

    if len(emails) > limit:
        lines.append(f"…and {len(emails) - limit} more matching message(s).")
    return "\n".join(lines).strip()


def _is_due_list_question(question: str) -> bool:
    """True only for clear 'what's due' refreshes — not every mention of 'deadline'."""
    q = question.lower().strip()
    if _asks_undated_tasks(q):
        return False
    # Follow-ups / analytical questions should go to full AI, not the due-list shortcut
    if any(
        w in q
        for w in (
            "why",
            "which",
            "what's that",
            "explain",
            "miss",
            "email",
            "from ",
            "who",
            "how many",
            "detail",
        )
    ):
        # Still allow pure due-list phrasing like "what are my deadlines today"
        if not any(w in q for w in ("today", "tomorrow", "this week", "due", "refresh", "pending")):
            return False
    # "what are ... deadlines today/tomorrow" is a due list
    if "what are" in q and ("deadline" in q or "due" in q):
        return True
    return any(
        w in q
        for w in (
            "what's due",
            "what is due",
            "what are my deadline",
            "any deadline",
            "pending deadline",
            "check deadline",
            "check pending",
            "refresh the deadline",
            "refresh deadline",
            "due today",
            "due tomorrow",
            "deadlines today",
            "deadlines tomorrow",
            "deadlines for",
            "deadline for today",
            "deadline for tomorrow",
            "need to do today",
            "need to do tomorrow",
            "tasks for today",
            "tasks for tomorrow",
        )
    ) or (
        ("deadline" in q or "due" in q or "pending" in q)
        and ("today" in q or "tomorrow" in q or "refresh" in q or "check" in q)
    )


def _is_simple_deadline_question(question: str) -> bool:
    """Backward-compatible alias used by Gmail query heuristics."""
    return _is_due_list_question(question) or (
        any(w in question.lower() for w in ("tomorrow", "today"))
        and "deadline" in question.lower()
    )


def _should_use_llm_queries(question: str) -> bool:
    """Rarely use LLM for Gmail query building — heuristics save free-tier tokens."""
    # Only when heuristics look weak: long topical ask with no keywords extracted
    keywords = _extract_keywords(question, max_words=8)
    if keywords:
        return False
    q = question.lower()
    if any(w in q for w in ("deadline", "due", "today", "tomorrow", "mail", "email", "from")):
        return False
    return len(question.split()) >= 8


def build_gmail_queries(
    question: str,
    pasted_email: str | None = None,
    *,
    use_llm: bool = True,
) -> list[str]:
    queries: list[str] = []

    if use_llm and _should_use_llm_queries(question):
        today = today_ist().isoformat()
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
    return unique[:4]


def _parse_target_date(question: str) -> datetime | None:
    """Resolve 'tomorrow' / 'today' / explicit dates in the question to IST midnight."""
    q = question.lower()
    ref = now_ist()
    if "tomorrow" in q:
        return (ref + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in q:
        return ref.replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan|january|feb|february|mar|march|apr|april|"
        r"may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|"
        r"nov|november|dec|december)(?:\s+(\d{4}))?",
        q,
    )
    if m:
        months = {
            "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
            "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
            "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
        }
        day, mon = int(m.group(1)), months[m.group(2)]
        year = int(m.group(3)) if m.group(3) else ref.year
        return datetime(year, mon, day, tzinfo=IST)

    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", q)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=IST)
    return None


def _fetch_relevant_emails(question: str, pasted_email: str | None = None) -> list[dict[str, Any]]:
    queries = build_gmail_queries(question, pasted_email=pasted_email)
    seen_ids: set[str] = set()
    emails: list[dict[str, Any]] = []
    wants_attachments = _wants_attachment_text(question)

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
                load_attach = wants_attachments and len(emails) < _MAX_ATTACHMENT_EMAILS
                details = get_message_details(message_id, load_attachments=load_attach)
            except Exception as exc:
                logger.warning("Fetching message %s failed: %s", message_id, exc)
                continue
            emails.append(
                _email_for_prompt(
                    details,
                    body_chars=_BODY_CHARS,
                    attachment_chars=_ATTACHMENT_CHARS_PER_EMAIL,
                    include_attachments=load_attach,
                )
            )
            if len(emails) >= _MAX_EMAILS:
                break

    if wants_attachments:
        emails = _apply_attachment_budget(emails, wants_attachments=True)
    return emails


def _item_deadline_dt(item: dict[str, Any]) -> datetime | None:
    # Anchor "tomorrow"/"today" to when the item was tracked (email time), not ask-time
    return item_effective_deadline_dt(item)


def _item_has_concrete_deadline(item: dict[str, Any]) -> bool:
    return item_effective_deadline_dt(item) is not None


def _deterministic_undated_answer(items: list[dict[str, Any]]) -> str:
    matched = [
        i
        for i in items
        if (not i.get("status") or i.get("status") == "open")
        and not _item_has_concrete_deadline(i)
    ]
    if not matched:
        return "No open tracked tasks without a concrete deadline right now."

    lines = [f"Open tasks with no concrete deadline ({len(matched)}):", ""]
    for i, item in enumerate(matched[:20], start=1):
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        text = item.get("deadline_text") or "—"
        lines.append(f"{i}. {task}")
        lines.append(f"   Course: {course}")
        lines.append(f"   Deadline text: {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _deterministic_deadline_answer(question: str, items: list[dict[str, Any]]) -> str | None:
    """Structured DB answers for clear due-list / undated-task questions."""
    q = question.lower()

    if _asks_undated_tasks(question):
        return _deterministic_undated_answer(items)

    if not _is_due_list_question(question) and _parse_target_date(question) is None:
        if not any(w in q for w in ("need to do today", "need to do tomorrow", "what's due", "refresh")):
            return None

    target = _parse_target_date(question)
    day_set: set | None = None
    if "today" in q and "tomorrow" in q:
        day_set = {today_ist(), today_ist() + timedelta(days=1)}
        label = "today and tomorrow (IST)"
    elif target is not None:
        day_set = {target.astimezone(IST).date()}
        label = target.strftime("%d %b %Y")
    elif _is_due_list_question(question):
        # Default refresh → today + tomorrow, not an unfiltered dump
        day_set = {today_ist(), today_ist() + timedelta(days=1)}
        label = "today and tomorrow (IST)"
    else:
        return None

    matched: list[dict[str, Any]] = []
    for item in items:
        if item.get("status") and item.get("status") != "open":
            continue
        dt = _item_deadline_dt(item)
        if day_set is not None:
            if dt is None or dt.astimezone(IST).date() not in day_set:
                continue
        matched.append(item)

    if not matched:
        return f"No open deadlines found for {label} in tracked items."

    lines = [f"Deadlines for {label} ({len(matched)}):", ""]
    for i, item in enumerate(matched[:15], start=1):
        task = item.get("task") or item.get("summary") or "Untitled"
        course = item.get("course_or_topic") or "—"
        due = format_deadline_display(item.get("deadline_date"), item.get("deadline_text"), tz=IST)
        lines.append(f"{i}. {task}")
        lines.append(f"   Course: {course}")
        lines.append(f"   Due (IST): {due}")
        lines.append("")
    return "\n".join(lines).strip()


def _slim_item_for_prompt(item: dict[str, Any]) -> dict[str, Any]:
    """Give the LLM IST display times — never raw UTC ISO for user-facing answers."""
    dt = item_effective_deadline_dt(item)
    due_day_ist = dt.astimezone(IST).strftime("%Y-%m-%d") if dt else None
    has_concrete = dt is not None
    due_ist = (
        format_deadline_display(item.get("deadline_date"), item.get("deadline_text"), tz=IST)
        if has_concrete
        else "No concrete deadline"
    )
    return {
        "category": item.get("category"),
        "course_or_topic": item.get("course_or_topic"),
        "task": item.get("task"),
        "deadline_text": item.get("deadline_text"),
        "has_concrete_deadline": has_concrete,
        "due_ist": due_ist,
        "due_day_ist": due_day_ist,
        "summary": item.get("summary"),
        "status": item.get("status"),
        "tracked_at": item.get("created_at"),
    }


def _build_user_prompt(
    question: str,
    items: list[dict[str, Any]],
    emails: list[dict[str, Any]],
    category: str | None,
    pasted_email: str | None,
    mailbox_stats: dict[str, Any] | None = None,
    *,
    verified: bool = False,
) -> str:
    scope = f"Category hint: {category}" if category else "Category hint: none"
    parts = [scope, ""]

    if pasted_email:
        parts.append("Student pasted this email text:")
        parts.append(pasted_email[:_PASTED_CHARS])
        parts.append("")

    if mailbox_stats:
        parts.append(f"Mailbox stats (JSON):\n{json.dumps(mailbox_stats, default=str)}")
        parts.append("")

    if not verified:
        slim_items = [_slim_item_for_prompt(i) for i in items[:_MAX_ITEMS]]
        # For pure mailbox questions, keep tracked items short to leave room for mail bodies
        item_limit = 8 if mailbox_stats else _MAX_ITEMS
        parts.append(
            f"Tracked items (JSON):\n{json.dumps(slim_items[:item_limit], default=str)}"
        )

    parts.append(f"\nMatching Gmail emails (JSON):\n{json.dumps(emails, default=str)}")

    header = "\n".join(parts)
    tail = f"\nStudent question:\n{question}"
    text = header + tail
    if len(text) > _MAX_USER_CHARS:
        truncate_note = "\n...[truncated]"
        budget = _MAX_USER_CHARS - len(tail) - len(truncate_note)
        if budget < 200:
            budget = 200
        header = header[:budget] + truncate_note
        text = header + tail
    return text


def _paste_fallback_answer(pasted: str) -> str:
    """Structured extract when LLM is unavailable."""
    lines = ["From the email you pasted:", ""]
    task_lines: list[str] = []
    for line in pasted.splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) < 8:
            continue
        lower = stripped.lower()
        if any(w in lower for w in ("read", "submit", "complete", "prepare", "due", "session", "case", "chapter", "assignment")):
            task_lines.append(stripped[:220])

    deadline_dt = None
    deadline_phrase = None
    for chunk in re.split(r"[.\n;]", pasted):
        chunk = chunk.strip()
        if not chunk:
            continue
        if re.search(r"tomorrow|today|\d{1,2}\s+\w+|due|deadline|session|\d{1,2}:\d{2}", chunk, re.I):
            resolved = resolve_deadline_text(chunk, now=now_ist())
            if resolved:
                deadline_dt = resolved
                deadline_phrase = chunk[:180]
                break

    if task_lines:
        for i, task in enumerate(task_lines[:6], start=1):
            lines.append(f"{i}. {task}")
    else:
        preview = pasted.strip().replace("\n", " ")[:400]
        lines.append(preview)

    if deadline_dt:
        lines.append("")
        shown = format_deadline_display(
            deadline_dt.isoformat(),
            deadline_phrase,
            tz=IST,
        )
        lines.append(f"Resolved deadline (IST): {shown}")

    lines.append("")
    lines.append("(Offline format — extracted from your paste.)")
    return "\n".join(lines)


def _answer_looks_generic_due_dump(answer: str, question: str) -> bool:
    """Detect when the model ignored a specific question and spat a due-list template."""
    if not _asks_undated_tasks(question) and "what are" not in question.lower():
        return False
    a = answer.lower()
    return (
        "for today" in a
        and "for tomorrow" in a
        and "do not have specified deadlines" in a
    )


def _llm_answer_is_useless(answer: str | None) -> bool:
    """Catch empty / confused model replies that ignore the user's real ask."""
    if not answer or not answer.strip():
        return True
    a = answer.lower().strip()
    needles = (
        "haven't asked a question",
        "have not asked a question",
        "you haven't asked",
        "please go ahead and ask",
        "what would you like to know",
        "ask your question",
        "ask a question",
        "please ask a question",
        "i'm ready when you are",
        "how can i help",
        "send me a question",
    )
    if any(n in a for n in needles):
        return True
    # Very short non-answers
    if len(a) < 40 and "?" in a and "deadline" not in a and "due" not in a:
        return True
    return False


def _looks_like_real_due_answer(answer: str) -> bool:
    a = answer.lower()
    return any(
        w in a
        for w in (
            "due",
            "deadline",
            "today",
            "tomorrow",
            "no open",
            "nothing due",
            "tracked",
            "course",
        )
    )


def _gather_context_for_question(
    question: str,
    *,
    scan_gmail: bool,
    pasted_email: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pull the richest useful Gmail context; heuristics only choose WHAT to fetch."""
    if not scan_gmail:
        return None, []

    q = question.lower()
    stats: dict[str, Any] | None = None
    emails: list[dict[str, Any]] = []

    wants_marks = _wants_attachment_text(question)
    wants_mailbox = (
        _is_mailbox_question(question)
        or bool(_extract_person_query(question))
        or wants_marks
        or any(w in q for w in ("mail", "email", "inbox", "sender", "whom", "who sent", "said", "wrote"))
    )
    wants_due = _is_due_list_question(question) or any(
        w in q for w in ("deadline", "due", "pending", "tomorrow", "today")
    )

    if wants_mailbox:
        try:
            stats, emails = _gather_mailbox_context(question)
        except Exception as exc:
            logger.warning("Mailbox context failed: %s", exc)
            stats, emails = {"count": 0, "error": str(exc)}, []
    else:
        try:
            emails = _fetch_relevant_emails(question, pasted_email=pasted_email)
        except Exception as exc:
            logger.warning("Relevant email fetch failed: %s", exc)
            emails = []

    # For due/today questions, also attach a light today-mail count so the model has mailbox awareness
    if wants_due and stats is None:
        try:
            day_q = _gmail_day_bounds_query()
            stats = count_messages(day_q, hard_cap=100)
            stats["note"] = "today_mail_count_for_context"
        except Exception:
            pass

    return stats, emails


def answer_question(
    question: str,
    items: list[dict[str, Any]],
    category: str | None = None,
    *,
    scan_gmail: bool = True,
    pasted_email: str | None = None,
    verified: bool = False,
) -> str:
    """LLM-first: always ask the model; keyword heuristics only gather context / fallback."""
    ref = now_ist()
    today = today_ist().isoformat()
    now_str = ref.strftime("%Y-%m-%d %H:%M %Z")

    if verified:
        system = VERIFIED_ASSISTANT_PROMPT.format(
            today=today, now=now_str, student_name=get_settings().student_name
        )
        try:
            stats, emails = _gather_mailbox_context(question, force_bodies=True)
        except Exception as exc:
            logger.warning("Verified mailbox fetch failed: %s", exc)
            stats, emails = {"count": 0, "error": str(exc)}, []
        user = _build_user_prompt(
            question,
            items,
            emails,
            category,
            pasted_email,
            mailbox_stats=stats,
            verified=True,
        )
        answer = call_text(system, user, temperature=0.45, max_tokens=1100)
        if answer and not _llm_answer_is_useless(answer):
            sources = _format_sources_block(emails)
            if sources:
                return f"{answer.strip()}\n\n{sources}"
            return answer.strip()
        return _format_mailbox_fallback(stats or {}, emails, question)

    system = ASSISTANT_PROMPT.format(
        today=today, now=now_str, student_name=get_settings().student_name
    )

    stats, emails = _gather_context_for_question(
        question, scan_gmail=scan_gmail, pasted_email=pasted_email
    )
    user = _build_user_prompt(
        question,
        items,
        emails,
        category,
        pasted_email,
        mailbox_stats=stats,
    )

    # Primary path: let the LLM understand any natural wording
    answer = call_text(system, user, temperature=0.55, max_tokens=1100)
    if answer and not _llm_answer_is_useless(answer):
        if _asks_undated_tasks(question) and _answer_looks_generic_due_dump(answer, question):
            return _deterministic_undated_answer(items)
        if emails and "no emails were received" in answer.lower():
            if stats:
                return _format_mailbox_fallback(stats, emails, question)
        return answer

    # ---- Fallbacks only when the model fails / refuses ----
    _, status = get_last_groq_error()

    if _asks_undated_tasks(question):
        return _deterministic_undated_answer(items)

    if _is_due_list_question(question) or any(
        w in question.lower() for w in ("deadline", "due", "pending", "today", "tomorrow")
    ):
        deterministic = _deterministic_deadline_answer(question, items)
        if deterministic:
            note = "\n\n(Offline format — tracked items from your DB.)"
            return deterministic + note

    if stats is not None or (
        scan_gmail and any(w in question.lower() for w in ("mail", "email", "from ", "inbox", "whom"))
    ):
        if stats is None:
            try:
                stats, emails = _gather_mailbox_context(question)
            except Exception:
                stats, emails = {"count": 0}, emails
        return _format_mailbox_fallback(stats or {}, emails, question)

    if pasted_email:
        return _paste_fallback_answer(pasted_email)

    if emails:
        lines = [f"Found {len(emails)} matching email(s):", ""]
        for i, mail in enumerate(emails[:8], start=1):
            lines.append(f"{i}. {mail.get('subject') or 'No subject'}")
            lines.append(f"   From: {mail.get('sender') or 'unknown'}")
            lines.append(f"   Received: {mail.get('received_at') or 'unknown'}")
            lines.append("")
        lines.append("(Offline format — listing Gmail matches.)")
        return "\n".join(lines).strip()

    deterministic = _deterministic_deadline_answer(question, items)
    if deterministic:
        return deterministic + "\n\n(Offline format — tracked items.)"

    return (
        "Couldn't reach any free AI provider right now, and I don't have matching "
        "mail/deadline data for that. Try /due or /classes, or ask again shortly."
    )


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
