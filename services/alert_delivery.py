"""Send actionable alerts with inline buttons and optional pin."""

from __future__ import annotations

import io
import logging
from typing import Any

from services.alert_actions import build_action_keyboard
from services.attachments import format_marks_for_telegram, should_forward_attachment
from services.formatting import escape_html
from services.telegram_dedupe import already_sent_content, record_content_sent
from services.telegram_util import deliver_message, send_html_detailed

logger = logging.getLogger(__name__)

_DEDUPE_SENTINEL = -1


async def send_actionable_alert(
    bot,
    chat_id: str,
    text: str,
    *,
    assignment_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    caption_prefix: str | None = None,
    pin: bool = False,
    show_reply: bool = False,
) -> int | None:
    """Send HTML alert; return message_id of first chunk only if all chunks sent.

    Pinning is intentionally disabled: auto-pin storms filled chats with hundreds
    of pins. Priority is still signaled in the message text (IMPORTANT prefix).
    ``show_reply`` adds a "Reply to sender" button — only use it for the owner DM
    where the seeded compose flow is available.
    """
    if not chat_id:
        return None
    if await already_sent_content(chat_id, text):
        return _DEDUPE_SENTINEL

    # Never auto-pin from the watcher/reminder paths — Telegram pin caps and
    # reprocessing loops turned this into hundreds of pinned messages.
    pin = False

    reply_markup = (
        build_action_keyboard(assignment_id, show_reply=show_reply) if assignment_id else None
    )
    from services.telegram_util import chunk_text

    pieces = chunk_text(text)
    sent_id: int | None = None
    all_ok = True

    for i, piece in enumerate(pieces):
        markup = reply_markup if i == len(pieces) - 1 else None

        async def _send(part: str, *, parse_mode: str | None = None):
            return await bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=parse_mode or "HTML",
                reply_markup=markup,
            )

        result = await deliver_message(_send, piece, parse_mode="HTML", allow_plain_fallback=True)
        if not result.ok:
            all_ok = False
            for chunk in result.chunks:
                if chunk.error:
                    logger.warning(
                        "send_actionable_alert to %s chunk %s failed: %s",
                        chat_id,
                        chunk.index,
                        chunk.error,
                    )
            break
        if sent_id is None:
            sent_id = result.first_message_id

    if sent_id and pin and all_ok:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_id, disable_notification=True)
        except Exception as exc:
            logger.warning("pin_chat_message failed for %s: %s", chat_id, exc)

    if not all_ok:
        return None

    await record_content_sent(chat_id, text)

    attach_ok = await deliver_attachments(
        bot,
        chat_id,
        attachments or [],
        caption_prefix=caption_prefix,
    )
    if not attach_ok:
        logger.warning(
            "send_actionable_alert to %s: text delivered but attachment forwarding failed",
            chat_id,
        )
    return sent_id


async def deliver_attachments(
    bot,
    chat_id: str,
    attachments: list[dict[str, Any]],
    *,
    caption_prefix: str | None = None,
) -> bool:
    """Marks as text; readings/other as files. True when all intended attachments were delivered."""
    if not attachments:
        return True

    attempted = 0
    sent = 0
    for att in attachments:
        intent = att.get("intent") or "other"
        filename = att.get("filename") or "attachment"
        extracted = (att.get("extracted_text") or "").strip()

        if intent == "marks" and extracted:
            attempted += 1
            try:
                ok = await send_html_detailed(
                    bot,
                    chat_id,
                    format_marks_for_telegram(filename, extracted),
                )
                if ok.ok:
                    sent += 1
            except Exception as exc:
                logger.warning("Failed sending marks text %s: %s", filename, exc)
            continue

        if not att.get("forward") and intent != "reading":
            continue
        if intent == "reading" or should_forward_attachment(att):
            data = att.get("data")
            if not data:
                continue
            attempted += 1
            caption = filename
            if caption_prefix:
                caption = f"{str(caption_prefix)[:80]}\n{filename}"
            try:
                from telegram import InputFile

                await bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(io.BytesIO(data), filename=filename),
                    caption=caption[:1000],
                )
                sent += 1
            except Exception as exc:
                logger.warning("Failed sending attachment %s to %s: %s", filename, chat_id, exc)
    return attempted == 0 or sent == attempted


async def send_owner_update(
    bot,
    chat_id: str,
    text: str,
    *,
    assignment_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    pin: bool = False,
    show_reply: bool = False,
) -> bool:
    if not chat_id:
        return False
    if assignment_id:
        msg_id = await send_actionable_alert(
            bot,
            chat_id,
            text,
            assignment_id=assignment_id,
            attachments=attachments,
            pin=pin,
            show_reply=show_reply,
        )
        return msg_id is not None
    if await already_sent_content(chat_id, text):
        return True
    result = await send_html_detailed(bot, chat_id, text)
    if not result.ok:
        return False
    await record_content_sent(chat_id, text)
    if attachments:
        return await deliver_attachments(bot, chat_id, attachments)
    return True
