"""In-memory conversation state for the single-user web client."""

from __future__ import annotations

from typing import Any

from services.conversation import MAX_HISTORY, remember_turn

_web_convo: dict[str, Any] = {
    "last_person": None,
    "last_person_email": None,
    "last_query": None,
    "last_intent": None,
    "history": [],
}

_web_compose: dict[str, Any] | None = None


def get_web_convo() -> dict[str, Any]:
    return _web_convo


def clear_web_convo() -> None:
    global _web_compose
    _web_convo.clear()
    _web_convo.update(
        {
            "last_person": None,
            "last_person_email": None,
            "last_query": None,
            "last_intent": None,
            "history": [],
        }
    )
    _web_compose = None


def remember_web_turn(
    *,
    user_text: str | None = None,
    bot_text: str | None = None,
    intent: str | None = None,
    person: str | None = None,
    person_email: str | None = None,
    query: str | None = None,
) -> None:
    remember_turn(
        {"convo": _web_convo},
        user_text=user_text,
        bot_text=bot_text,
        intent=intent,
        person=person,
        person_email=person_email,
        query=query,
    )


def chat_history_for_api(*, limit: int = MAX_HISTORY) -> list[dict[str, str]]:
    history = _web_convo.get("history") or []
    return history[-limit:]


def get_web_compose() -> dict[str, Any] | None:
    return _web_compose


def set_web_compose(data: dict[str, Any] | None) -> None:
    global _web_compose
    _web_compose = data
