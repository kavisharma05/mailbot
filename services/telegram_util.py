"""Helpers for sending Telegram messages safely.

Telegram rejects any message longer than 4096 characters. Digests, timetable
shares and Q&A answers can grow past that, so everything user-facing goes
through here to be chunked on line boundaries and sent defensively (one bad
chunk never aborts the rest, and never bubbles up to kill a watcher run).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Telegram's hard limit is 4096; leave headroom so a trailing tag/word is safe.
MAX_MESSAGE_CHARS = 3900


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into <=limit pieces, preferring line boundaries.

    Each formatted line in this app carries self-contained HTML tags
    (e.g. ``<b>...</b>``), so splitting between lines keeps tags balanced.
    A single over-long line is hard-split as a last resort.
    """
    if text is None:
        return []
    text = str(text)
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # Line itself longer than the limit — flush and hard-split it.
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current.strip():
        chunks.append(current)
    return chunks


async def send_html(
    bot,
    chat_id: str | int,
    text: str,
    **kwargs: Any,
) -> bool:
    """Send an HTML message, chunked if needed. Returns True if anything sent.

    Swallows send errors (logs them) so a single failure never aborts a
    watcher run or a digest.
    """
    if not chat_id or not text or not text.strip():
        return False

    sent_any = False
    for piece in chunk_text(text):
        try:
            await bot.send_message(chat_id=chat_id, text=piece, parse_mode="HTML", **kwargs)
            sent_any = True
        except Exception as exc:
            logger.warning("send_message to %s failed: %s", chat_id, exc)
    return sent_any


async def reply_long(message, text: str, **kwargs: Any) -> None:
    """Reply to a Telegram message, chunked if needed. Never raises on length."""
    if not text or not text.strip():
        return
    for piece in chunk_text(text):
        try:
            await message.reply_text(piece, **kwargs)
        except Exception as exc:
            logger.warning("reply_text failed: %s", exc)
