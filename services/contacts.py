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
_CONNECTOR_TOKENS = {"or", "and", "of", "name"}
GENERIC_PERSON_WORDS = {
    "faculty",
    "professor",
    "teacher",
    "someone",
    "somebody",
    "person",
    "prof",
    "friend",
    "friendly",
    "buddy",
    "pal",
    "recipient",
    "you",
    "me",
    "us",
    "myself",
    "yourself",
    "everyone",
    "everybody",
    "anyone",
    "anybody",
    "group",
    "class",
    "mail",
    "email",
    "message",
    "msg",
    "the faculty",
    "a faculty",
    "the professor",
    "a professor",
    "the teacher",
    "a teacher",
    "my professor",
    "my faculty",
    "my friend",
    "a friend",
    "any faculty",
    "any professor",
    "any teacher",
    "any friend",
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


_PRONOUN_REFERENCES = frozenset(
    {
        "her",
        "him",
        "them",
        "she",
        "he",
        "they",
        "same",
        "same person",
        "that person",
        "the same",
        "same one",
        "the same person",
        "the same one",
        "unko",
        "unki",
        "unhe",
        "usko",
        "uski",
        "usne",
        "inko",
        "inhe",
    }
)
_HONORIFIC_ONLY = frozenset({"sir", "mam", "maam", "ma'am", "madam", "ma"})
_EMAIL_IN_TEXT_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_FROM_PERSON_RE = re.compile(
    r"\b(?:mails?|emails?)\s+from\s+(.+)$",
    re.IGNORECASE,
)
_TO_FOR_SPLIT_RE = re.compile(r"\b(?:to|for)\s+", re.IGNORECASE)
_SKIP_TO_FOR_TAIL = re.compile(
    r"^(?:the\s+)?(?:class|assignment|deadline|mail|email|report|test|exam|students?)\b",
    re.IGNORECASE,
)
_PRONOUN_IN_TEXT_RE = re.compile(
    r"\b(?:her|him|them|she|he|they|unko|unki|unhe|usko|uski|inko|inhe)\b",
    re.IGNORECASE,
)
_SAME_PERSON_RE = re.compile(
    r"\b(?:the\s+)?same(?:\s+(?:person|one|people))?\b|\bthat person\b",
    re.IGNORECASE,
)
_SEND_PERSON_A_MAIL_RE = re.compile(
    r"\b(?:send|write|draft|drop|shoot|fire)\s+(.+?)\s+(?:a|an|the|that|this)\s+(?:mail|email)\b",
    re.IGNORECASE,
)
_KO_MAIL_RE = re.compile(
    r"(.+?)\s+ko\s+(?:ek\s+)?(?:mail|email|msg)\b",
    re.IGNORECASE,
)
_COMMAND_LEFTOVER_RE = re.compile(
    r"\b(?:send|write|draft|compose|reply|forward|saying|please|kindly|"
    r"remind|deadline|class(?:es)?|want|need|can|could|would)\b",
    re.IGNORECASE,
)
_PRONOUN_STRIP_RE = re.compile(
    r"\b(?:send|write|draft|compose|reply|email|mail|forward|drop|shoot|please|kindly|"
    r"can|you|could|want|wanna|need|have|to|a|an|the|it|this|that|one|saying|about|"
    r"regarding|with|for|of|and|or|her|him|them|she|he|they|unko|unki|unhe|"
    r"usko|uski|inko|inhe|same|person|one|ko|bhej|bhejo|bhejna|karo|kar|"
    r"instead|actually|just|message|msg|from|received|got|we|i|do|dena)\b",
    re.IGNORECASE,
)
_RECIPIENT_SWITCH_RE = re.compile(
    r"\b(?:instead|not (?:her|him|them)|change (?:the )?(?:recipient|to)|"
    r"wrong (?:person|recipient)|actually (?:send|mail|email) (?:it |this )?to)\b",
    re.IGNORECASE,
)
_SHORT_SEND_TO_RE = re.compile(
    r"\b(?:send|mail|email|forward|write)\b.+\bto\b|\b(?:mail|email)\s+[\w.'-]+",
    re.IGNORECASE,
)


def clean_person_phrase(raw: str | None) -> str | None:
    """Strip leftover command words so 'saleena mam please' becomes a usable name."""
    if not raw:
        return None
    text = raw.strip(" .,?!:;\"'")
    text = re.sub(r"^(?:a|an|the|my|our|this|that)\s+", "", text, flags=re.I)
    text = re.sub(
        r"^(?:send|write|draft|compose|mail|email|message|msg|check|see|show|list|get|find|open)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.split(
        r"\s+(?:saying|that|about|regarding|with|please|pls|asap|now|instead|"
        r"bhej(?:o|na)?|kar(?:o|na)?|do|dena|ke liye)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0]
    text = re.sub(r"\s+ko\s*$", "", text, flags=re.I)
    text = re.sub(
        r"\s+(?:a|an|the)?\s*(?:mail|email|message|msg)s?\s*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" .,?!:;")
    if not text:
        return None
    if is_generic_person(text):
        return None
    return text


def _last_person_if_pronoun(text: str | None, last_person: str | None) -> str | None:
    """Map her/him/same person (alone or inside a command) to last_person."""
    if not text or not last_person:
        return None
    lowered = text.lower().strip(" .,?!")
    if lowered in _PRONOUN_REFERENCES or lowered in _HONORIFIC_ONLY:
        return last_person
    if _SAME_PERSON_RE.search(lowered):
        leftover = _PRONOUN_STRIP_RE.sub(" ", lowered)
        leftover = re.sub(r"\s+", " ", leftover).strip(" .,?!")
        if not leftover or is_generic_person(leftover):
            return last_person
    if _PRONOUN_IN_TEXT_RE.search(lowered):
        leftover = _PRONOUN_STRIP_RE.sub(" ", lowered)
        leftover = re.sub(r"\s+", " ", leftover).strip(" .,?!")
        if not leftover or is_generic_person(leftover):
            return last_person
    return None


def looks_like_recipient_switch(text: str | None) -> bool:
    """True when the user is changing or naming who the mail should go to."""
    if not text or not str(text).strip():
        return False
    raw = str(text).strip()
    if _RECIPIENT_SWITCH_RE.search(raw):
        return True
    if len(raw.split()) <= 6 and (
        _SHORT_SEND_TO_RE.search(raw)
        or re.search(r"\b(?:send|write|draft|mail|email)\b.+\b(?:mail|email)\b", raw, re.I)
    ):
        return True
    return False


def extract_person_reference(text: str | None, convo: dict[str, Any] | None = None) -> str | None:
    """Pull the intended person out of a casual reply or compose sentence."""
    if not text or not str(text).strip():
        return None
    raw = str(text).strip()
    email_hit = _EMAIL_IN_TEXT_RE.search(raw)
    if email_hit:
        return email_hit.group(1).lower()

    last_person = (convo or {}).get("last_person") if convo else None
    lowered = raw.lower().strip(" .,?!")
    if lowered in _PRONOUN_REFERENCES or lowered in _HONORIFIC_ONLY:
        return last_person or None

    from_match = _FROM_PERSON_RE.search(raw)
    if from_match:
        cleaned = clean_person_phrase(from_match.group(1))
        return _last_person_if_pronoun(cleaned or from_match.group(1), last_person) or cleaned or last_person

    ko_match = _KO_MAIL_RE.search(raw)
    if ko_match:
        cleaned = clean_person_phrase(ko_match.group(1))
        pronoun = _last_person_if_pronoun(cleaned or ko_match.group(1), last_person)
        if pronoun:
            return pronoun
        if cleaned and not _COMMAND_LEFTOVER_RE.search(cleaned):
            return cleaned

    send_match = _SEND_PERSON_A_MAIL_RE.search(raw)
    if send_match:
        cleaned = clean_person_phrase(send_match.group(1))
        pronoun = _last_person_if_pronoun(cleaned or send_match.group(1), last_person)
        if pronoun:
            return pronoun
        if cleaned and not _COMMAND_LEFTOVER_RE.search(cleaned):
            return cleaned

    for part in reversed(_TO_FOR_SPLIT_RE.split(raw)[1:]):
        if _SKIP_TO_FOR_TAIL.match(part.strip()):
            continue
        cleaned = clean_person_phrase(part)
        pronoun = _last_person_if_pronoun(cleaned or part, last_person)
        if pronoun:
            return pronoun
        if cleaned and not _COMMAND_LEFTOVER_RE.search(cleaned):
            return cleaned

    pronoun = _last_person_if_pronoun(raw, last_person)
    if pronoun:
        return pronoun

    tokens = raw.split()
    if 1 <= len(tokens) <= 5:
        if "?" in raw or re.search(
            r"\b(?:what|when|who|how|why|which|where)'?s?\b|\b(?:due|deadline|today|tomorrow)\b",
            lowered,
        ):
            return None
        cleaned = clean_person_phrase(raw)
        if cleaned and not _COMMAND_LEFTOVER_RE.search(cleaned):
            return cleaned
    return None


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
    meaningful = [t for t in tokens if t not in _CONNECTOR_TOKENS]
    if not meaningful:
        return True
    if all(token in GENERIC_PERSON_WORDS for token in meaningful):
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
        return None, [], "Who should I send it to? Tell me their name or email address."

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
