"""Hold a WhatsApp digest until the owner opens the 24h session window.

Meta allows free-form messages only after the user writes first. We do not
fight that: if a digest cannot be delivered, we stash the full text and
(optionally) send an approved utility-template nudge. The stash is delivered
when the owner replies (any message opens the 24h window) or sends DIGEST.
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
STASH_MAX_CHARS = 3800
_STASH_SEP = "\n\n— — —\n\n"
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


def _trim_stash_text(combined: str) -> str:
    """Keep the newest content when over the WhatsApp body budget."""
    text = combined.strip()
    if len(text) <= STASH_MAX_CHARS:
        return text
    tail = text[-STASH_MAX_CHARS:]
    sep_at = tail.find(_STASH_SEP)
    if 0 <= sep_at < 120:
        trimmed = tail[sep_at + len(_STASH_SEP) :].strip()
        return trimmed or tail.strip()
    return tail.strip()


def _teaser(kind: str) -> str:
    if kind == "morning":
        return "Morning digest ready. Reply DIGEST to open it."
    if kind == "evening":
        return "Evening digest ready. Reply DIGEST to open it."
    return "A saved briefing is waiting. Reply DIGEST to open it."


def pending_digest_line(kind: str) -> str:
    if kind == "morning":
        return "Your morning briefing is saved. Reply DIGEST to read it."
    if kind == "evening":
        return "Your evening briefing is saved. Reply DIGEST to read it."
    return "A saved briefing is waiting. Reply DIGEST to read it."


async def announce_pending_digest(phone: str, user_data: dict[str, Any]) -> bool:
    """Mention a stashed digest once. Does not send the full text."""
    pending = user_data.get(PENDING_KEY)
    if not isinstance(pending, dict) or not pending.get("text") or pending.get("announced"):
        return False
    ok = await get_whatsapp_client().send_text(
        phone,
        pending_digest_line(str(pending.get("kind") or "")),
        from_html=False,
        template_fallback=False,
    )
    if ok:
        pending["announced"] = True
    return ok


async def stash_pending_digest(phone: str, *, kind: str, text: str) -> None:
    data = await load_user_data(phone)
    existing = data.get(PENDING_KEY)
    now = datetime.now(timezone.utc).isoformat()
    new_body = (text or "").strip()
    if isinstance(existing, dict) and existing.get("text") and new_body:
        existing_kind = str(existing.get("kind") or "")
        if existing_kind == kind:
            combined = new_body
        else:
            combined = _trim_stash_text(
                f"{str(existing['text']).rstrip()}{_STASH_SEP}{new_body}"
            )
        data[PENDING_KEY] = {
            "kind": kind,
            "text": _trim_stash_text(combined),
            "stashed_at": now,
            "announced": False,
        }
    elif new_body:
        data[PENDING_KEY] = {
            "kind": kind,
            "text": _trim_stash_text(new_body),
            "stashed_at": now,
            "announced": False,
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
    ok = await client.send_text(
        phone,
        str(pending["text"]),
        from_html=True,
        template_fallback=False,
        outbox_kind=str(pending.get("kind") or "digest"),
    )
    if not ok:
        data[PENDING_KEY] = pending
        logger.warning("Could not flush pending WhatsApp %s digest for %s", pending.get("kind"), phone)
    if owned:
        await save_user_data(phone, data)
    return ok


async def stash_and_nudge(phone: str, *, kind: str, text: str) -> bool:
    """Stash content for later and send the approved template nudge.

    Returns True when the template nudge was sent. The stashed text is sent
    on the owner's next inbound message (or when they send DIGEST).
    """
    await stash_pending_digest(phone, kind=kind, text=text)
    tmpl = digest_template_name()
    if not tmpl:
        logger.info(
            "WhatsApp %s stashed for %s (no utility template configured)",
            kind,
            phone,
        )
        return False
    # Static digest templates have no {{1}}; alert templates need a teaser.
    digest_only = (get_settings().whatsapp_digest_template or "").strip()
    body = None if digest_only else _teaser(kind)
    nudged = await get_whatsapp_client().send_template(phone, tmpl, body_parameter=body)
    logger.info(
        "WhatsApp %s stashed for %s; template nudge %s",
        kind,
        phone,
        "sent" if nudged else "failed",
    )
    return nudged


async def deliver_whatsapp_digest(text: str, *, kind: str) -> str:
    """Try free-form digest; stash (+ optional template nudge) if the window is closed.

    Returns ``sent``, ``stashed``, or ``skipped``.
    """
    client = get_whatsapp_client()
    phone = _owner_phone()
    if not client.enabled or not phone or not (text or "").strip():
        return "skipped"

    ok = await client.send_text(
        phone, text, from_html=True, template_fallback=False, outbox_kind=kind
    )
    if ok:
        return "sent"

    await stash_and_nudge(phone, kind=kind, text=text)
    return "stashed"
