"""Hold a WhatsApp digest until the owner opens the 24h session window.

Meta allows free-form messages only after the user writes first. We do not
fight that: if a digest cannot be delivered, we stash the full text and
(optionally) send an approved utility-template nudge. The next inbound
message from the owner opens the window and we deliver the stash.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config import get_settings
from services.messaging.state import load_user_data, save_user_data
from services.messaging.whatsapp import get_whatsapp_client

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_whatsapp_digest"
DIGEST_KEYWORDS = {
    "digest",
    "full digest",
    "show digest",
    "send digest",
    "/digest",
}


def digest_template_name() -> str:
    settings = get_settings()
    return (
        (settings.whatsapp_digest_template or "").strip()
        or (settings.whatsapp_alert_template or "").strip()
    )


def _owner_phone() -> str:
    return (get_settings().whatsapp_owner_phone or "").lstrip("+").replace(" ", "").replace("-", "")


def _teaser(kind: str) -> str:
    label = "Morning" if kind == "morning" else "Evening"
    return f"{label} digest ready. Reply DIGEST or tap Menu to open it."


async def stash_pending_digest(phone: str, *, kind: str, text: str) -> None:
    data = await load_user_data(phone)
    data[PENDING_KEY] = {
        "kind": kind,
        "text": text,
        "stashed_at": datetime.now(timezone.utc).isoformat(),
    }
    await save_user_data(phone, data)


def take_pending_digest(user_data: dict[str, Any]) -> dict[str, Any] | None:
    pending = user_data.pop(PENDING_KEY, None)
    return pending if isinstance(pending, dict) and pending.get("text") else None


async def flush_pending_digest(phone: str, user_data: dict[str, Any] | None = None) -> bool:
    """Send a stashed digest now that the owner has written in. Returns True if sent."""
    owned = user_data is None
    data = user_data if user_data is not None else await load_user_data(phone)
    pending = take_pending_digest(data)
    if not pending:
        return False
    client = get_whatsapp_client()
    ok = await client.send_text(phone, str(pending["text"]), from_html=True, template_fallback=False)
    if not ok:
        data[PENDING_KEY] = pending
        logger.warning("Could not flush pending WhatsApp %s digest for %s", pending.get("kind"), phone)
    if owned:
        await save_user_data(phone, data)
    return ok


async def deliver_whatsapp_digest(text: str, *, kind: str) -> str:
    """Try free-form digest; stash (+ optional template nudge) if the window is closed.

    Returns ``sent``, ``stashed``, or ``skipped``.
    """
    client = get_whatsapp_client()
    phone = _owner_phone()
    if not client.enabled or not phone or not (text or "").strip():
        return "skipped"

    ok = await client.send_text(phone, text, from_html=True, template_fallback=False)
    if ok:
        return "sent"

    await stash_pending_digest(phone, kind=kind, text=text)
    tmpl = digest_template_name()
    if tmpl:
        # Static digest templates have no {{1}}; alert templates need a teaser.
        digest_only = (get_settings().whatsapp_digest_template or "").strip()
        body = None if digest_only else _teaser(kind)
        nudged = await client.send_template(phone, tmpl, body_parameter=body)
        logger.info(
            "WhatsApp %s digest stashed for %s; template nudge %s",
            kind,
            phone,
            "sent" if nudged else "failed",
        )
    else:
        logger.info(
            "WhatsApp %s digest stashed for %s (no utility template configured)",
            kind,
            phone,
        )
    return "stashed"
