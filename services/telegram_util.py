"""Helpers for sending Telegram messages safely.

Telegram rejects any message longer than 4096 characters. Digests, timetable
shares and Q&A answers can grow past that, so everything user-facing goes
through here to be chunked on line boundaries and sent defensively.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from services.formatting import escape_html

logger = logging.getLogger(__name__)

# Telegram's hard limit is 4096; leave headroom so a trailing tag/word is safe.
MAX_MESSAGE_CHARS = 3900
MAX_SEND_RETRIES = 3
RETRY_BASE_DELAY = 0.5

_TRANSIENT_RE = re.compile(
    r"(429|500|502|503|504|timeout|timed out|connection|network|flood|retry)",
    re.IGNORECASE,
)
_HTML_PARSE_RE = re.compile(r"can't parse entities|parse entities|entity", re.IGNORECASE)


@dataclass
class ChunkDelivery:
    index: int
    sent: bool
    message_id: int | None = None
    error: str | None = None
    used_plain_fallback: bool = False


@dataclass
class DeliveryResult:
    ok: bool
    chunks_attempted: int = 0
    chunks_sent: int = 0
    first_message_id: int | None = None
    chunks: list[ChunkDelivery] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.chunks_sent > 0 and self.chunks_sent < self.chunks_attempted


def _find_safe_hard_split(text: str, start: int, limit: int) -> int:
    """Pick a split index <= start+limit, avoiding cuts inside HTML tags."""
    end = min(start + limit, len(text))
    if end >= len(text):
        return end

    in_tag = False
    last_space: int | None = None
    for i in range(start, end):
        ch = text[i]
        if ch == "<":
            in_tag = True
        elif ch == ">":
            in_tag = False
        elif ch in " \t" and not in_tag:
            last_space = i

    if last_space is not None and last_space > start + (limit // 3):
        return last_space

    if "<" in text[start:end]:
        close = text.find(">", end)
        if close != -1 and close - start <= limit + 200:
            return close + 1

    return end


def _hard_split_line(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    chunks: list[str] = []
    start = 0
    while start < len(line):
        end = _find_safe_hard_split(line, start, limit)
        if end <= start:
            end = min(start + limit, len(line))
        chunks.append(line[start:end].rstrip())
        start = end
        if start < len(line) and line[start] in " \t":
            start += 1
    return [piece for piece in chunks if piece]


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into <=limit pieces, preferring line boundaries."""
    if text is None:
        return []
    text = str(text)
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split_line(line, limit))
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


def _is_transient_error(exc: Exception) -> bool:
    return bool(_TRANSIENT_RE.search(str(exc)))


def _is_html_parse_error(exc: Exception) -> bool:
    return bool(_HTML_PARSE_RE.search(str(exc)))


def _strip_html_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def _send_piece_with_retry(
    send_fn,
    piece: str,
    *,
    parse_mode: str | None,
    allow_plain_fallback: bool,
) -> tuple[bool, int | None, str | None, bool]:
    last_error: str | None = None
    used_plain = False
    mode = parse_mode

    for attempt in range(1, MAX_SEND_RETRIES + 1):
        try:
            msg = await send_fn(piece, parse_mode=mode) if mode else await send_fn(piece)
            return True, getattr(msg, "message_id", None), None, used_plain
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = str(exc)
            if "WhatsApp send failed" in last_error:
                raise
            if mode == "HTML" and allow_plain_fallback and _is_html_parse_error(exc):
                logger.warning("HTML parse failed; retrying chunk as plain text")
                piece = _strip_html_tags(piece)
                mode = None
                used_plain = True
                continue
            if attempt < MAX_SEND_RETRIES and _is_transient_error(exc):
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            return False, None, last_error, used_plain
    return False, None, last_error, used_plain


async def deliver_message(
    send_fn,
    text: str,
    *,
    parse_mode: str | None = "HTML",
    allow_plain_fallback: bool = True,
) -> DeliveryResult:
    """Send text in chunks with retry and optional plain-text fallback."""
    pieces = chunk_text(text)
    result = DeliveryResult(ok=False, chunks_attempted=len(pieces))
    if not pieces:
        return result

    for index, piece in enumerate(pieces):
        sent, message_id, error, used_plain = await _send_piece_with_retry(
            send_fn,
            piece,
            parse_mode=parse_mode,
            allow_plain_fallback=allow_plain_fallback,
        )
        chunk = ChunkDelivery(
            index=index,
            sent=sent,
            message_id=message_id,
            error=error,
            used_plain_fallback=used_plain,
        )
        result.chunks.append(chunk)
        if sent:
            result.chunks_sent += 1
            if result.first_message_id is None:
                result.first_message_id = message_id

    result.ok = result.chunks_sent == result.chunks_attempted and result.chunks_attempted > 0
    return result


async def send_html(
    bot,
    chat_id: str | int,
    text: str,
    **kwargs: Any,
) -> bool:
    """Send an HTML message, chunked if needed. Returns True only if all chunks sent."""
    if not chat_id or not text or not text.strip():
        return False

    async def _send(piece: str, *, parse_mode: str | None = None):
        return await bot.send_message(
            chat_id=chat_id,
            text=piece,
            parse_mode=parse_mode or "HTML",
            **kwargs,
        )

    result = await deliver_message(_send, text, parse_mode="HTML", allow_plain_fallback=True)
    if not result.ok:
        for chunk in result.chunks:
            if chunk.error:
                logger.warning("send_message to %s chunk %s failed: %s", chat_id, chunk.index, chunk.error)
    return result.ok


async def send_html_detailed(
    bot,
    chat_id: str | int,
    text: str,
    **kwargs: Any,
) -> DeliveryResult:
    if not chat_id or not text or not text.strip():
        return DeliveryResult(ok=False)

    async def _send(piece: str, *, parse_mode: str | None = None):
        return await bot.send_message(
            chat_id=chat_id,
            text=piece,
            parse_mode=parse_mode or "HTML",
            **kwargs,
        )

    return await deliver_message(_send, text, parse_mode="HTML", allow_plain_fallback=True)


async def reply_long(message, text: str, **kwargs: Any) -> DeliveryResult:
    """Reply to a Telegram message, chunked if needed."""
    if not text or not text.strip():
        return DeliveryResult(ok=False)

    parse_mode = kwargs.pop("parse_mode", None)
    allow_plain_fallback = parse_mode == "HTML"

    async def _send(piece: str, *, parse_mode: str | None = None):
        if parse_mode:
            return await message.reply_text(piece, parse_mode=parse_mode, **kwargs)
        return await message.reply_text(piece, **kwargs)

    result = await deliver_message(
        _send,
        text,
        parse_mode=parse_mode,
        allow_plain_fallback=allow_plain_fallback,
    )
    if not result.ok:
        for chunk in result.chunks:
            if chunk.error:
                logger.warning("reply_text chunk %s failed: %s", chunk.index, chunk.error)
    return result


def safe_user_error_message(action: str = "complete that request") -> str:
    return f"Sorry — I couldn't {action} right now. Please try again in a minute."


def plain_answer(text: str) -> str:
    """Normalize plain LLM output for Telegram (no HTML)."""
    if not text:
        return ""
    return escape_html(text) if "<" in text or ">" in text else text.strip()
