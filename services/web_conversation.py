"""Per-user in-memory conversation state for the web client."""

from __future__ import annotations

from typing import Any

from services.conversation import MAX_HISTORY, remember_turn
from services.user_ids import OWNER_SEED_USER_ID

_default_convo: dict[str, Any] = {
    "last_person": None,
    "last_person_email": None,
    "last_query": None,
    "last_intent": None,
    "history": [],
}

_web_convos: dict[str, dict[str, Any]] = {}
_web_compose_by_user: dict[str, dict[str, Any] | None] = {}


def _uid(user_id: str | None) -> str:
    if user_id:
        return user_id
    from services.mailbox_user import current, user_id as mailbox_id

    if current() is not None:
        return mailbox_id()
    return OWNER_SEED_USER_ID


def get_web_convo(user_id: str | None = None) -> dict[str, Any]:
    uid = _uid(user_id)
    if uid not in _web_convos:
        _web_convos[uid] = {**_default_convo, "history": []}
    return _web_convos[uid]


def clear_web_convo(user_id: str | None = None) -> None:
    uid = _uid(user_id)
    _web_convos[uid] = {**_default_convo, "history": []}
    _web_compose_by_user[uid] = None


def remember_web_turn(
    *,
    user_text: str | None = None,
    bot_text: str | None = None,
    intent: str | None = None,
    person: str | None = None,
    person_email: str | None = None,
    query: str | None = None,
    user_id: str | None = None,
) -> None:
    remember_turn(
        {"convo": get_web_convo(user_id)},
        user_text=user_text,
        bot_text=bot_text,
        intent=intent,
        person=person,
        person_email=person_email,
        query=query,
    )


def chat_history_for_api(*, limit: int = MAX_HISTORY, user_id: str | None = None) -> list[dict[str, str]]:
    history = get_web_convo(user_id).get("history") or []
    return history[-limit:]


def get_web_compose(user_id: str | None = None) -> dict[str, Any] | None:
    return _web_compose_by_user.get(_uid(user_id))


def set_web_compose(data: dict[str, Any] | None, user_id: str | None = None) -> None:
    _web_compose_by_user[_uid(user_id)] = data
