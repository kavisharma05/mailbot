"""Meta WhatsApp 24h customer-service window (per user phone session)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

SERVICE_WINDOW_HOURS = 24
_LAST_INBOUND_KEY = "last_whatsapp_inbound_at"


def touch_service_window(user_data: dict[str, Any]) -> None:
    """Call on every inbound WhatsApp message from this phone."""
    user_data[_LAST_INBOUND_KEY] = datetime.now(timezone.utc).isoformat()


def service_window_open(user_data: dict[str, Any] | None) -> bool:
    """True when free-form Cloud API messages are allowed (user wrote recently)."""
    if not user_data:
        return False
    raw = user_data.get(_LAST_INBOUND_KEY)
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ts < timedelta(hours=SERVICE_WINDOW_HOURS)
