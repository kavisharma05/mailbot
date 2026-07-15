import json
import logging
from datetime import datetime
from typing import Any

from ai.client import call_json, call_text
from ai.prompts import ASSISTANT_PROMPT, GMAIL_QUERY_PROMPT
from gmail_api.client import get_message_details, search_message_ids

logger = logging.getLogger(__name__)

_MAX_EMAILS = 6
_BODY_CHARS = 1200


def build_gmail_query(question: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    system = GMAIL_QUERY_PROMPT.format(today=today)
    result = call_json(system, f"Question:\n{question}")
    if result and isinstance(result.get("query"), str) and result["query"].strip():
        return result["query"].strip()
    # Fallback: raw keywords over the last 60 days
    return f"{question} newer_than:60d"


def _fetch_relevant_emails(question: str) -> list[dict[str, Any]]:
    try:
        query = build_gmail_query(question)
        message_ids = search_message_ids(query, max_results=_MAX_EMAILS)
    except Exception as exc:
        logger.warning("Gmail search failed: %s", exc)
        return []

    emails: list[dict[str, Any]] = []
    for message_id in message_ids:
        try:
            details = get_message_details(message_id)
        except Exception as exc:
            logger.warning("Fetching message %s failed: %s", message_id, exc)
            continue
        emails.append(
            {
                "subject": details.get("subject"),
                "sender": details.get("sender"),
                "body": (details.get("body") or "")[:_BODY_CHARS],
            }
        )
    return emails


def answer_question(
    question: str,
    items: list[dict[str, Any]],
    category: str | None = None,
    *,
    scan_gmail: bool = True,
) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    system = ASSISTANT_PROMPT.format(today=today)

    emails = _fetch_relevant_emails(question) if scan_gmail else []
    scope = f"Category filter: {category}" if category else "Category filter: all"
    user = (
        f"{scope}\n\n"
        f"Tracked open items (JSON):\n{json.dumps(items, default=str)}\n\n"
        f"Recent matching emails (JSON):\n{json.dumps(emails, default=str)}\n\n"
        f"Student question:\n{question}"
    )
    answer = call_text(system, user)
    if not answer:
        return "I couldn't generate an answer right now. Try /due or /list."
    return answer
