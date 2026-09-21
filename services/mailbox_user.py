"""Per-request mailbox user context for Gmail watcher and send paths."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from config import Settings, get_settings
from services.user_ids import OWNER_SEED_USER_ID
from services.users import is_owner_user

_active_user: ContextVar[dict[str, Any] | None] = ContextVar("active_mailbox_user", default=None)


def set_user(user: dict[str, Any]):
    return _active_user.set(user)


def reset(token) -> None:
    _active_user.reset(token)


def current() -> dict[str, Any] | None:
    return _active_user.get()


def user_id() -> str:
    u = current()
    return str(u["id"]) if u else OWNER_SEED_USER_ID


def telegram_dm_chat_id(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    u = current()
    if not u:
        return settings.telegram_owner_chat_id or ""
    if is_owner_user(u):
        return settings.telegram_owner_chat_id or str(u.get("telegram_chat_id") or "")
    return str(u.get("telegram_chat_id") or "")


def gmail_address(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    u = current()
    if u and u.get("gmail_address"):
        return str(u["gmail_address"]).strip()
    return (settings.gmail_user or "").strip()


def use_category_groups() -> bool:
    u = current()
    return bool(u and is_owner_user(u))
