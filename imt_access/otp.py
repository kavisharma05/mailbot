"""Read MapMyAccess OTP codes from the linked Gmail inbox.

Only used as a fallback: the portal's direct login endpoint normally returns a
session token without OTP. If the portal ever switches the account to the OTP
flow, we poll Gmail for the freshest code instead of bothering the owner.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_OTP_RE = re.compile(r"\b(\d{4,8})\b")
_OTP_CONTEXT_RE = re.compile(
    r"(?:otp|one[- ]time|verification|authentication|code)\D{0,40}?(\d{4,8})\b",
    re.IGNORECASE,
)


def _looks_like_year(code: str) -> bool:
    return len(code) == 4 and code[:2] in {"19", "20"}


def extract_otp(text: str) -> str | None:
    """Pull the most likely OTP code out of an email body."""
    if not text:
        return None
    for match in _OTP_CONTEXT_RE.finditer(text):
        code = match.group(1)
        if not _looks_like_year(code):
            return code
    # Fallback: first standalone 4-8 digit number that is not a year
    for match in _OTP_RE.finditer(text):
        code = match.group(1)
        if _looks_like_year(code):
            continue
        return code
    return None


def fetch_otp_from_gmail(
    *,
    max_age_minutes: int = 10,
    timeout_seconds: int = 120,
    poll_seconds: int = 8,
    not_before: float | None = None,
) -> str | None:
    """Poll Gmail for a fresh MapMyAccess OTP email. Returns the code or None."""
    from gmail_api.client import get_message_details, search_message_ids

    started = time.monotonic()
    started_wall = time.time()
    reference = not_before if not_before is not None else started_wall - max_age_minutes * 60
    query = "(from:mapmyaccess OR subject:mapmyaccess OR subject:otp) newer_than:1d"
    seen: set[str] = set()

    while time.monotonic() - started < timeout_seconds:
        try:
            ids = search_message_ids(query, max_results=8)
            for message_id in ids:
                if message_id in seen:
                    continue
                seen.add(message_id)
                details: dict[str, Any] = get_message_details(
                    message_id, load_attachments=False
                )
                received_iso = str(details.get("received_at_iso") or "")
                if received_iso:
                    try:
                        from datetime import datetime

                        received_ts = datetime.fromisoformat(received_iso).timestamp()
                        if received_ts < reference:
                            continue
                    except ValueError:
                        pass
                body = " ".join(
                    part
                    for part in (
                        str(details.get("subject") or ""),
                        str(details.get("body") or "")[:4000],
                    )
                    if part
                )
                code = extract_otp(body)
                if code:
                    logger.info("Found MapMyAccess OTP in Gmail message %s", message_id)
                    return code
        except Exception as exc:
            logger.warning("Gmail OTP poll failed: %s", exc)
        time.sleep(poll_seconds)

    logger.warning("No MapMyAccess OTP arrived in Gmail within %ss", timeout_seconds)
    return None
