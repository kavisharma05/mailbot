"""Short-term conversation memory for Telegram chat turns."""

from __future__ import annotations

from typing import Any

from services.contacts import canonical_person_name

MAX_HISTORY = 12


def get_convo(user_data: dict[str, Any]) -> dict[str, Any]:
    """Return the conversation memory dict, creating it if needed."""
    convo = user_data.get("convo")
    if not isinstance(convo, dict):
        convo = {
            "last_person": None,
            "last_person_email": None,
            "last_query": None,
            "last_intent": None,
            "history": [],
        }
        user_data["convo"] = convo
    return convo


def clear_convo(user_data: dict[str, Any]) -> None:
    """Drop conversation memory and any in-progress compose draft."""
    user_data.pop("convo", None)
    user_data.pop("compose", None)
    user_data.pop("last_intent", None)
    user_data.pop("last_schedule_day", None)


def remember_turn(
    user_data: dict[str, Any],
    *,
    user_text: str | None = None,
    bot_text: str | None = None,
    intent: str | None = None,
    person: str | None = None,
    person_email: str | None = None,
    query: str | None = None,
) -> None:
    """Record a user/bot exchange and optional entity focus."""
    convo = get_convo(user_data)
    history: list[dict[str, str]] = convo.setdefault("history", [])

    if user_text:
        history.append({"role": "user", "text": user_text.strip()[:500]})
    if bot_text:
        history.append({"role": "bot", "text": bot_text.strip()[:800]})

    if len(history) > MAX_HISTORY * 2:
        convo["history"] = history[-(MAX_HISTORY * 2) :]

    if intent:
        convo["last_intent"] = intent
        user_data["last_intent"] = intent
    if person:
        convo["last_person"] = canonical_person_name(person) or person.strip()
    if person_email:
        convo["last_person_email"] = person_email
    if query:
        convo["last_query"] = query


def history_for_prompt(convo: dict[str, Any] | None, *, limit: int = MAX_HISTORY) -> list[dict[str, str]]:
    """Recent turns for LLM / intent context."""
    if not convo:
        return []
    history = convo.get("history") or []
    return history[-limit:]
