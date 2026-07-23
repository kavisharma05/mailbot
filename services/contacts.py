"""Learned sender contacts from Gmail (name -> email resolution)."""

from __future__ import annotations

import logging
import re
from typing import Any

from services.db import find_contacts_by_name, upsert_contact as db_upsert_contact

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_TITLE_PREFIXES = {"dr", "prof", "professor", "mr", "mrs", "ms", "miss"}
_HONORIFICS = {"mam", "ma'am", "maam", "sir"}


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


def normalize_name_key(name: str | None) -> str:
    """Lowercase searchable key with honorifics/titles stripped."""
    if not name:
        return ""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    cleaned = [t for t in tokens if t not in _TITLE_PREFIXES and t not in _HONORIFICS]
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
    return any(tok in hay for tok in req_tokens)


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
    hits = sum(1 for t in query_tokens if t in hay)
    if hits == 0:
        return 0.0
    ratio = hits / len(query_tokens)
    count_boost = min(int(row.get("message_count") or 0), 50) / 100.0
    return ratio + count_boost


def resolve_contact(name: str | None, *, min_score: float = 0.5) -> list[dict[str, Any]]:
    """Return ranked contact matches for a casual name like 'santhi mam'."""
    if not name or "@" in name:
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
