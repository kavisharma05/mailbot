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
    try:
        _active_user.reset(token)
    except ValueError:
        _active_user.set(None)


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
    # A classmate with no mailbox yet must not inherit the owner's Gmail address.
    if u and not is_owner_user(u):
        return ""
    return (settings.gmail_user or "").strip()


def student_name(settings: Settings | None = None) -> str:
    """Name used in drafted mail and marksheet matching for the active user."""
    settings = settings or get_settings()
    configured = (settings.student_name or "").strip()
    if configured.startswith("["):
        configured = ""
    u = current()
    if u and is_owner_user(u):
        return configured or _pretty_person_name(str(u.get("display_name") or "")) or "Student"
    if u:
        from services.users import (
            display_name_is_telegram_handle,
            refresh_display_name_from_gmail,
            stored_name_should_come_from_gmail,
        )

        if stored_name_should_come_from_gmail(u):
            u = refresh_display_name_from_gmail(u) or u
        name = _pretty_person_name(str(u.get("display_name") or "").strip())
        if (
            name
            and name.lower() not in {"student", "owner"}
            and not display_name_is_telegram_handle(name, u)
        ):
            return name
        gmail = str(u.get("gmail_address") or "").strip()
        local = gmail.split("@", 1)[0] if "@" in gmail else ""
        pretty_mail = _pretty_person_name(local)
        if pretty_mail and not pretty_mail.isdigit():
            return pretty_mail
        return "Student"
    return configured or "Student"


def _pretty_person_name(name: str) -> str:
    cleaned = " ".join(name.replace("_", " ").replace(".", " ").split())
    if cleaned.isalpha() or all(part.isalpha() for part in cleaned.split()):
        return cleaned.title()
    return cleaned


def use_category_groups() -> bool:
    u = current()
    return bool(u and is_owner_user(u))
