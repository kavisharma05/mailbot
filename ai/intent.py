"""Hybrid intent classification: compose vs retrieve vs contact lookup."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ai.client import call_json
from ai.prompts import INTENT_PROMPT
from services.contacts import canonical_person_name
from services.conversation import history_for_prompt, MAX_HISTORY

logger = logging.getLogger(__name__)

INTENT_COMPOSE = "compose"
INTENT_CONTACT_LOOKUP = "contact_lookup"
INTENT_MAILBOX_QA = "mailbox_qa"
INTENT_SCHEDULE = "schedule"
INTENT_DUE_LIST = "due_list"
INTENT_SMALLTALK = "smalltalk"

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")

_COMPOSE_VERBS = (
    "send",
    "write",
    "draft",
    "compose",
    "reply",
    "email",
    "mail",
)
_RETRIEVE_MARKERS = (
    "from ",
    "what did",
    "show",
    "list",
    "how many",
    "count",
    "summar",
    "what were",
    "those mails",
    "the mails",
    "her mails",
    "his mails",
    "their mails",
)
_SCHEDULE_KEYWORDS = ("class", "classes", "schedule", "timetable", "time table", "lecture")
_DUE_KEYWORDS = (
    "what's due",
    "what is due",
    "due today",
    "due tomorrow",
    "due this week",
    "deadlines today",
    "deadlines tomorrow",
    "deadline for today",
    "deadline for tomorrow",
    "what are my deadline",
    "check deadline",
    "refresh deadline",
    "pending deadline",
    "open items",
    "what do i need to do",
)


def _extract_email(text: str) -> str | None:
    m = _EMAIL_RE.search(text)
    return m.group(1).lower() if m else None


def _extract_person_after_preposition(text: str, prep: str) -> str | None:
    m = re.search(
        rf"\b{prep}\s+([A-Za-z0-9._%+-@]+(?:\s+[A-Za-z][A-Za-z.'-]*){{0,3}})",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    name = m.group(1).strip(" .,?!")
    if "@" in name:
        return name
    parts = [
        p
        for p in name.split()
        if p.lower() not in {"the", "a", "an", "my", "your", "her", "his", "their"}
    ]
    joined = " ".join(parts) if parts else None
    return canonical_person_name(joined) if joined else None


def _extract_compose_content(text: str) -> str | None:
    patterns = [
        r"\bsaying\s+(.+)$",
        r"\bthat\s+(.+)$",
        r"\bcontent\s*[:\-]\s*(.+)$",
        r"\bmessage\s*[:\-]\s*(.+)$",
        r"\bbody\s*[:\-]\s*(.+)$",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            content = m.group(1).strip()
            if len(content) >= 8:
                return content
    return None


def _is_contact_lookup(text: str) -> tuple[bool, str | None]:
    q = text.lower()
    patterns = [
        r"(?:what(?:'s| is)|tell me|give me|find|get)\s+(?:the\s+)?(?:email|mail(?:\s+id)?|address)\s+(?:of|for)\s+(.+?)[\?\.!]*$",
        r"(.+?)(?:'s|\s+)email(?:\s+address|\s+id)?[\?\.!]*$",
        r"(?:email|mail(?:\s+id)?)\s+(?:of|for)\s+(.+?)[\?\.!]*$",
    ]
    for pat in patterns:
        m = re.search(pat, q, flags=re.IGNORECASE)
        if m:
            person = m.group(1).strip(" .,?!")
            person = re.sub(r"\b(mam|ma'am|maam|sir|dr|prof|professor)\b\.?", "", person, flags=re.I).strip()
            if person and len(person) >= 2:
                return True, person
    return False, None


def _is_compose_intent(text: str) -> tuple[bool, str | None, str | None, str | None]:
    q = text.lower().strip()
    email = _extract_email(text)

    # "send ... to X" / "mail to X"
    m = re.search(
        r"\b(?:send|write|draft|compose|reply|email|mail)\b.+\bto\b\s+(.+?)(?:\s+(?:saying|that|with|about)\b|$)",
        text,
        flags=re.IGNORECASE,
    )
    if m:
        recipient = m.group(1).strip(" .,?!")
        content = _extract_compose_content(text)
        if email and not recipient:
            recipient = email
        return True, recipient or email, content, email

    # "mail X saying ..."
    m = re.search(
        r"\b(?:send|write|draft|compose|reply|email|mail)\s+(?:a\s+)?(?:mail|email)?\s*(?:to\s+)?([^\n]+?)\s+(?:saying|that)\s+(.+)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        recipient = m.group(1).strip(" .,?!")
        content = m.group(2).strip()
        return True, recipient, content, email

    # bare "/send-like" follow-up content after recipient was given
    if email and any(v in q for v in _COMPOSE_VERBS):
        return True, email, _extract_compose_content(text) or None, email

    return False, None, None, email


def _is_retrieval_intent(text: str) -> bool:
    q = text.lower()
    if any(m in q for m in _RETRIEVE_MARKERS):
        return True
    if re.search(r"\b(?:mails?|emails?)\s+(?:from|by)\b", q):
        return True
    if re.search(r"\b(?:from|by)\s+[A-Za-z][A-Za-z.'-]{2,}", q) and not re.search(
        r"\b(?:send|write|draft|compose|reply|email|mail)\b.+\bto\b", q, re.I
    ):
        return True
    if re.search(r"\bwhat did\b.+\b(?:say|write|mention|send)\b", q):
        return True
    # "send all mails from X" is retrieval, not compose
    if "send" in q and re.search(r"\b(?:all\s+)?(?:mails?|emails?)\s+from\b", q):
        return True
    return False


def _is_schedule_intent(text: str, last_intent: str | None = None) -> bool:
    q = text.lower()
    if any(k in q for k in _SCHEDULE_KEYWORDS):
        return True
    if last_intent == "schedule" and any(w in q for w in ("check again", "refresh", "look again")):
        return True
    return False


def _is_specific_deadline_question(text: str) -> bool:
    """Single-item deadline Q&A — not a /due list refresh."""
    from ai.assistant import _is_due_list_question

    if _is_due_list_question(text):
        return False
    q = text.lower()
    return any(
        p in q
        for p in (
            "when is",
            "when's",
            "what is the deadline",
            "what's the deadline",
            "deadline for the",
            "due date for",
            "due date of",
        )
    ) and ("deadline" in q or "due date" in q or " due " in f" {q} ")


def _is_due_intent(text: str) -> bool:
    from ai.assistant import _is_due_list_question

    return _is_due_list_question(text)


def _llm_classify(text: str, convo: dict[str, Any] | None) -> dict[str, Any] | None:
    history = history_for_prompt(convo, limit=MAX_HISTORY)
    payload = {
        "recent_history": history,
        "message": text,
        "last_person": (convo or {}).get("last_person"),
        "last_query": (convo or {}).get("last_query"),
    }
    try:
        result = call_json(INTENT_PROMPT, json.dumps(payload, default=str))
    except Exception as exc:
        logger.warning("LLM intent classify failed: %s", exc)
        return None
    if not result or not isinstance(result.get("intent"), str):
        return None
    return result


def classify_message(text: str, convo: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Return {intent, recipient, person, content, confidence}.
    Deterministic fast-path first; LLM only when ambiguous.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return {"intent": INTENT_SMALLTALK, "recipient": None, "person": None, "content": None, "confidence": "high"}

    q = cleaned.lower()
    last_intent = (convo or {}).get("last_intent")

    # Active compose follow-ups are handled in handlers; still detect content-only turns
    is_compose, recipient, content, email = _is_compose_intent(cleaned)
    is_lookup, lookup_person = _is_contact_lookup(cleaned)

    if is_lookup and not is_compose:
        return {
            "intent": INTENT_CONTACT_LOOKUP,
            "recipient": None,
            "person": lookup_person,
            "content": None,
            "confidence": "high",
        }

    if is_compose:
        # "send all mails from X" is retrieval, not outbound compose
        if re.search(r"\b(?:all\s+)?(?:mails?|emails?)\s+from\b", q):
            pass
        else:
            return {
                "intent": INTENT_COMPOSE,
                "recipient": recipient or email,
                "person": None,
                "content": content,
                "confidence": "high",
            }

    if _is_retrieval_intent(cleaned) and not is_compose:
        person = _extract_person_after_preposition(cleaned, "from") or _extract_person_after_preposition(
            cleaned, "by"
        )
        return {
            "intent": INTENT_MAILBOX_QA,
            "recipient": None,
            "person": person,
            "content": None,
            "confidence": "high",
        }

    if _is_schedule_intent(cleaned, last_intent=last_intent):
        return {
            "intent": INTENT_SCHEDULE,
            "recipient": None,
            "person": None,
            "content": None,
            "confidence": "high",
        }

    if _is_specific_deadline_question(cleaned):
        return {
            "intent": INTENT_MAILBOX_QA,
            "recipient": None,
            "person": None,
            "content": None,
            "confidence": "high",
        }

    if _is_due_intent(cleaned):
        return {
            "intent": INTENT_DUE_LIST,
            "recipient": None,
            "person": None,
            "content": None,
            "confidence": "high",
        }

    if cleaned.lower().rstrip("!.?") in {"hi", "hey", "hello", "yo", "sup", "hii", "hiii", "heya"}:
        return {
            "intent": INTENT_SMALLTALK,
            "recipient": None,
            "person": None,
            "content": None,
            "confidence": "high",
        }

    # Ambiguous — LLM fallback
    llm = _llm_classify(cleaned, convo)
    if llm:
        intent = llm.get("intent", INTENT_MAILBOX_QA)
        if intent == INTENT_DUE_LIST and _is_specific_deadline_question(cleaned):
            intent = INTENT_MAILBOX_QA
        if intent not in {
            INTENT_COMPOSE,
            INTENT_CONTACT_LOOKUP,
            INTENT_MAILBOX_QA,
            INTENT_SCHEDULE,
            INTENT_DUE_LIST,
            INTENT_SMALLTALK,
        }:
            intent = INTENT_MAILBOX_QA
        return {
            "intent": intent,
            "recipient": llm.get("recipient"),
            "person": llm.get("person"),
            "content": llm.get("content"),
            "confidence": llm.get("confidence") or "medium",
        }

    return {
        "intent": INTENT_MAILBOX_QA,
        "recipient": None,
        "person": None,
        "content": None,
        "confidence": "low",
    }
