"""Signed OAuth state tokens for Gmail reconnect."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from config import get_settings

STATE_TTL_SECONDS = 600


def create_oauth_state() -> str:
    settings = get_settings()
    nonce = secrets.token_urlsafe(16)
    ts = str(int(time.time()))
    payload = f"{nonce}:{ts}"
    sig = hmac.new(
        settings.task_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{ts}.{sig}"


def verify_oauth_state(state: str | None) -> bool:
    if not state:
        return False
    parts = state.split(".")
    if len(parts) != 3:
        return False
    nonce, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if time.time() - ts > STATE_TTL_SECONDS:
        return False
    settings = get_settings()
    expected = hmac.new(
        settings.task_secret.encode(),
        f"{nonce}:{ts_str}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig)
