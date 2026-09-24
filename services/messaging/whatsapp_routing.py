"""Map inbound WhatsApp phones to Mailbot users (owner + invited peers).

Outbound owner alerts/digests stay on WHATSAPP_OWNER_PHONE only — see alert_delivery
and pending_digest._owner_phone().
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any

from config import get_settings
from services.messaging.phone import normalize_wa_digits, phone_to_stable_user_id, wa_phones_match
from services.users import (
    get_owner_user,
    get_user_by_id,
    get_user_by_username,
    list_users,
)

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _owner_phone_digits() -> str:
    return normalize_wa_digits(get_settings().whatsapp_owner_phone or "")


@lru_cache
def _phone_to_user_key() -> dict[str, str]:
    """Normalized phone -> lookup key (username, user id, or display-name hint)."""
    raw = (get_settings().whatsapp_phone_map or "").strip()
    out: dict[str, str] = {}
    if not raw:
        return out
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece or ":" not in piece:
            continue
        phone_part, key_part = piece.split(":", 1)
        phone = normalize_wa_digits(phone_part)
        key = key_part.strip()
        if phone and key:
            out[phone] = key
    return out


def clear_whatsapp_routing_cache() -> None:
    _phone_to_user_key.cache_clear()


def _resolve_map_key(key: str) -> dict[str, Any] | None:
    k = (key or "").strip()
    if not k:
        return None
    if _UUID_RE.match(k):
        return get_user_by_id(k)
    by_user = get_user_by_username(k)
    if by_user:
        return by_user
    hint = k.lower()
    for row in list_users(active_only=True):
        name = str(row.get("display_name") or "").lower()
        if hint in name.split() or name.startswith(hint):
            return row
    logger.warning("WhatsApp phone map key %r did not match any active user", key)
    return None


def _legacy_owner_only_whatsapp() -> bool:
    return not _owner_phone_digits() and not _phone_to_user_key()


def resolve_whatsapp_bot_user(phone: str) -> dict[str, Any] | None:
    """User row for an allowed inbound WhatsApp number, or None if unknown."""
    normalized = normalize_wa_digits(phone)
    if not normalized:
        return None

    if _legacy_owner_only_whatsapp():
        return get_owner_user()

    owner = _owner_phone_digits()
    if owner and wa_phones_match(normalized, owner):
        return get_owner_user()

    key = _phone_to_user_key().get(normalized)
    if key:
        user = _resolve_map_key(key)
        if user:
            return user
        logger.warning("WhatsApp map for %s points at %r but user was not found", normalized, key)
        return None

    return None


def whatsapp_phone_allowed(phone: str) -> bool:
    """True when this inbound number may use the WhatsApp bot."""
    normalized = normalize_wa_digits(phone)
    if not normalized:
        return False
    if _legacy_owner_only_whatsapp():
        return True
    return resolve_whatsapp_bot_user(normalized) is not None


def whatsapp_actor_telegram_id(phone: str) -> int:
    """PTB effective_user.id for handlers — Telegram id when known, else stable phone id."""
    settings = get_settings()
    owner_id = settings.owner_user_id() if hasattr(settings, "owner_user_id") else ""
    user = resolve_whatsapp_bot_user(phone)
    if user and str(user.get("role") or "") == "owner" and str(owner_id).lstrip("-").isdigit():
        return int(owner_id)
    if user:
        raw = str(user.get("telegram_user_id") or "").strip()
        if raw.lstrip("-").isdigit():
            return int(raw)
    if str(owner_id).lstrip("-").isdigit():
        return int(owner_id)
    return phone_to_stable_user_id(phone)
