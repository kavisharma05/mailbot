"""Signed OAuth state tokens for Gmail reconnect."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from config import get_settings
from services.user_ids import OWNER_SEED_USER_ID

STATE_TTL_SECONDS = 600


def create_oauth_state(user_id: str | None = None) -> str:
    settings = get_settings()
    nonce = secrets.token_urlsafe(16)
    ts = str(int(time.time()))
    uid = (user_id or OWNER_SEED_USER_ID).strip()
    payload = f"{nonce}:{ts}:{uid}"
    sig = hmac.new(
        settings.task_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{ts}.{sig}.{uid}"


def verify_oauth_state(state: str | None) -> tuple[bool, str | None]:
    """Return (ok, user_id for token storage)."""
    if not state:
        return False, None
    parts = state.split(".")
    if len(parts) < 3:
        return False, None
    settings = get_settings()
    if len(parts) == 3:
        nonce, ts_str, sig = parts
        uid = OWNER_SEED_USER_ID
        payload = f"{nonce}:{ts_str}"
    else:
        nonce, ts_str, sig = parts[0], parts[1], parts[2]
        uid = parts[3]
        payload = f"{nonce}:{ts_str}:{uid}"
    try:
        ts = int(ts_str)
    except ValueError:
        return False, None
    if time.time() - ts > STATE_TTL_SECONDS:
        return False, None
    expected = hmac.new(
        settings.task_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, None
    return True, uid
