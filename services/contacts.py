"""Learned sender contacts from Gmail (name -> email resolution)."""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

from services.db import find_contacts_by_name, upsert_contact as db_upsert_contact

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_TITLE_PREFIXES = {
    "dr",
    "prof",
    "professor",
    "mr",
    "mrs",
    "ms",
    "miss",
    "sri",
    "shri",
}
_HONORIFICS = {
    "mam",
    "ma'am",
    "maam",
    "sir",
    "madam",
    "teacher",
    "faculty",
    "ma",
    "am",
    "ji",
}
_ARTICLE_TOKENS = {"a", "an", "the", "my", "your", "any", "some"}
GENERIC_PERSON_WORDS = {
    "faculty",
    "professor",
    "teacher",
    "someone",
    "somebody",
    "person",
    "prof",
    "the faculty",
    "a faculty",
    "the professor",
    "a professor",
    "the teacher",
    "a teacher",
    "my professor",
    "my faculty",
    "any faculty",
    "any professor",
    "any teacher",
}


def parse_sender(sender: str | None) -> tuple[str | None, str | None]:
    """Parse 'Display Name <email@x.com>' or bare email."""
    if not sender:
        return None, None
    text = sender.strip()
    m = re.match(r"^(.*?)\s*<([^>]+)>$", text)
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
        return (name or None), email
    m = _EMAIL_RE.search(text)
    if m:
        return None, m.group(1).lower()
    return text or None, None


def parse_address_list(header_value: str | None) -> list[tuple[str | None, str]]:
    """Parse comma-separated To/Cc header into (display_name, email) pairs."""
    if not header_value:
        return []
    out: list[tuple[str | None, str]] = []
    for chunk in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", header_value):
        chunk = chunk.strip()
        if not chunk:
            continue
        display, email = parse_sender(chunk)
        if email:
            out.append((display, email))
    return out


def normalize_name_key(name: str | None) -> str:
    """Lowercase searchable key with honorifics/titles stripped."""
    if not name:
        return ""
    text = name.lower()
    text = re.sub(r"ma\s*['']?\s*am", " ", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    cleaned = [
        t
        for t in tokens
        if t not in _TITLE_PREFIXES and t not in _HONORIFICS and t not in _ARTICLE_TOKENS
    ]
    return " ".join(cleaned)


def name_tokens(name: str | None) -> list[str]:
    key = normalize_name_key(name)
    return [t for t in key.split() if len(t) >= 2]


def canonical_person_name(name: str | None) -> str | None:
    """Normalize a casual person reference to a stable key (no honorifics/titles)."""
    if not name:
        return None
    text = name.strip()
    if not text:
        return None
    if "@" in text:
        return text.lower()
    key = normalize_name_key(text)
    return key or None


def is_generic_person(name: str | None) -> bool:
    """True when the phrase is not a specific person (e.g. 'a faculty')."""
    if not name:
        return True
    text = name.strip()
    if "@" in text:
        return False
    key = normalize_name_key(text)
    if not key:
        return True
    if key in GENERIC_PERSON_WORDS:
        return True
    tokens = [t for t in key.split() if t not in _ARTICLE_TOKENS]
    if not tokens:
        return True
    if all(token in GENERIC_PERSON_WORDS for token in tokens):
        return True
    return False


def fuzzy_token_hit(token: str, haystack: str, *, threshold: float = 0.75) -> bool:
    """True when token exactly or fuzzily matches a token in haystack."""
    if not token or not haystack:
        return False
    if token in haystack:
        return True
    hay_tokens = haystack.split()
    for hay_token in hay_tokens:
        if len(token) < 3 or len(hay_token) < 3:
            if token == hay_token:
                return True
            continue
        if SequenceMatcher(None, token, hay_token).ratio() >= threshold:
            return True
    return False


def _token_hits(query_tokens: list[str], hay: str) -> int:
    return sum(1 for token in query_tokens if token in hay or fuzzy_token_hit(token, hay))


def _gather_contact_rows(tokens: list[str]) -> list[dict[str, Any]]:
    """Union contact rows matching any name token, deduped by email."""
    seen_emails: set[str] = set()
    rows: list[dict[str, Any]] = []
    for token in tokens:
        for row in find_contacts_by_name(token):
            email = (row.get("email") or "").lower()
            if email and email not in seen_emails:
                seen_emails.add(email)
                rows.append(row)
    return rows


def sender_matches_name(sender_field: str | None, requested_name: str | None) -> bool:
    """True when sender display/email tokens overlap the requested person name."""
    if not requested_name:
        return True
    req_tokens = name_tokens(requested_name)
    if not req_tokens:
        return True
    display, email = parse_sender(sender_field)
    hay = " ".join(
        filter(
            None,
            [
                normalize_name_key(display),
                (email or "").split("@")[0].replace(".", " ").replace("_", " "),
            ],
        )
    )
    if not hay.strip():
        return False
    return _token_hits(req_tokens, hay) > 0


def recipient_matches_name(recipient_field: str | None, requested_name: str | None) -> bool:
    """True when a To/Cc header value matches the requested person name."""
    if not recipient_field or not requested_name:
        return False
    for display, email in parse_address_list(recipient_field):
        if sender_matches_name(f"{display or ''} <{email}>", requested_name):
            return True
    return False


def upsert_contact_from_sender(sender: str | None) -> None:
    display, email = parse_sender(sender)
    if not email:
        return
    name_key = normalize_name_key(display) or normalize_name_key(email.split("@")[0])
    try:
        db_upsert_contact(email=email, display_name=display, name_key=name_key)
    except Exception as exc:
        logger.warning("upsert_contact failed for %s: %s", email, exc)


def _score_contact(row: dict[str, Any], query_tokens: list[str]) -> float:
    key = (row.get("name_key") or "").lower()
    display = (row.get("display_name") or "").lower()
    email_local = (row.get("email") or "").split("@")[0].lower().replace(".", " ")
    hay = f"{key} {display} {email_local}"
    if not query_tokens:
        return 0.0
    hits = _token_hits(query_tokens, hay)
    if hits == 0:
        return 0.0
    ratio = hits / len(query_tokens)
    count_boost = min(int(row.get("message_count") or 0), 50) / 100.0
    return ratio + count_boost


def resolve_contact(name: str | None, *, min_score: float = 0.5) -> list[dict[str, Any]]:
    """Return ranked contact matches for a casual name like 'santhi mam'."""
    if not name or "@" in name or is_generic_person(name):
        return []
    tokens = name_tokens(name)
    if not tokens:
        return []
    rows = _gather_contact_rows(tokens)
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        score = _score_contact(row, tokens)
        if score >= min_score and sender_matches_name(
            f"{row.get('display_name') or ''} <{row.get('email')}>",
            name,
        ):
            scored.append((score, row))
    scored.sort(key=lambda x: (-x[0], -(x[1].get("message_count") or 0)))
    return [row for _, row in scored[:5]]


def resolve_contact_email(name: str | None) -> str | None:
    matches = resolve_contact(name)
    if len(matches) == 1:
        return matches[0].get("email")
    return None


def format_contact_lookup_answer(name: str, matches: list[dict[str, Any]]) -> str:
    if not matches:
        return (
            f"I couldn't find an email address for {name} in your inbox history yet. "
            "Try asking after you've received mail from them, or use their full email address."
        )
    if len(matches) == 1:
        row = matches[0]
        display = row.get("display_name") or name
        email = row.get("email")
        return f"{display} — {email}"
    lines = [f"Multiple matches for {name}:", ""]
    for i, row in enumerate(matches[:5], start=1):
        display = row.get("display_name") or row.get("email")
        lines.append(f"{i}. {display} — {row.get('email')}")
    lines.append("")
    lines.append("Reply with the number or paste the email address to use.")
    return "\n".join(lines)


def resolve_email_robust(
    name: str | None,
    *,
    convo: dict[str, Any] | None = None,
    emails: list[dict[str, Any]] | None = None,
) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """
    Best-effort email resolution: bare email -> DB -> convo memory -> live Gmail -> LLM.
    Returns (email, candidates, note).
    """
    if not name or not name.strip():
        return None, [], None

    text = name.strip()
    if "@" in text:
        return text.lower(), [], None

    if is_generic_person(text):
        return None, [], "Which faculty? Tell me their name or email."

    matches = resolve_contact(text)
    if len(matches) == 1:
        return matches[0].get("email"), matches, None
    if len(matches) > 1:
        return None, matches, format_contact_lookup_answer(text, matches)

    if convo:
        last_email = convo.get("last_person_email")
        last_person = convo.get("last_person")
        if last_email and last_person:
            if sender_matches_name(f"{last_person} <{last_email}>", text):
                return last_email, [], None
            req = normalize_name_key(text)
            last = normalize_name_key(last_person)
            if req and last and (
                req in last
                or last in req
                or any(fuzzy_token_hit(tok, last) for tok in name_tokens(text))
            ):
                return last_email, [], None

    # Lazy imports to avoid circular dependency with ai.assistant
    from ai.assistant import infer_person_email, llm_pick_contact, resolve_person_email_live

    if emails:
        live = infer_person_email(text, emails=emails, convo=convo)
        if live:
            return live, [], None

    live = resolve_person_email_live(text)
    if live:
        return live, [], None

    candidates = resolve_contact(text, min_score=0.35)
    if not candidates:
        # Re-gather broader DB hits for LLM disambiguation
        tokens = name_tokens(text)
        seen: set[str] = set()
        for token in tokens:
            for row in find_contacts_by_name(token):
                email = (row.get("email") or "").lower()
                if email and email not in seen:
                    seen.add(email)
                    candidates.append(row)

    picked = llm_pick_contact(text, candidates)
    if picked:
        return picked, candidates, None

    if candidates:
        return None, candidates, format_contact_lookup_answer(text, candidates)

    return None, [], (
        f"I couldn't find an email address for {text} in your inbox history yet. "
        "Try asking after you've received mail from them, or use their full email address."
    )


def backfill_contacts_from_inbox(*, days: int = 365, max_messages: int = 200) -> dict[str, int]:
    """Scan recent inbox senders and upsert contacts (one-time/manual admin use)."""
    from gmail_api.client import list_message_senders

    query = f"in:inbox newer_than:{days}d"
    try:
        batch = list_message_senders(query, max_results=max_messages)
    except Exception as exc:
        logger.warning("backfill_contacts_from_inbox failed: %s", exc)
        return {"scanned": 0, "upserted": 0, "error": 1}

    upserted = 0
    seen: set[str] = set()
    for mail in batch:
        sender = mail.get("sender")
        _, email = parse_sender(sender)
        if not email or email in seen:
            continue
        seen.add(email)
        upsert_contact_from_sender(sender)
        upserted += 1

    return {"scanned": len(batch), "upserted": upserted, "error": 0}
