"""Send actionable alerts with inline buttons and optional pin."""

from __future__ import annotations

import io
import logging
from typing import Any

from services.alert_actions import build_action_keyboard
from services.attachments import format_marks_for_telegram, should_forward_attachment
from services.formatting import escape_html
from services.telegram_util import send_html

logger = logging.getLogger(__name__)


async def send_actionable_alert(
    bot,
    chat_id: str,
    text: str,
    *,
    assignment_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    caption_prefix: str | None = None,
    pin: bool = False,
) -> int | None:
    """Send HTML alert; return message_id of first chunk if sent."""
    if not chat_id:
        return None

    reply_markup = build_action_keyboard(assignment_id) if assignment_id else None
    sent_id: int | None = None

    from services.telegram_util import chunk_text

    pieces = chunk_text(text)
    for i, piece in enumerate(pieces):
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=piece,
                parse_mode="HTML",
                reply_markup=reply_markup if i == len(pieces) - 1 else None,
            )
            if sent_id is None:
                sent_id = msg.message_id
        except Exception as exc:
            logger.warning("send_actionable_alert to %s failed: %s", chat_id, exc)

    if sent_id and pin:
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=sent_id, disable_notification=True)
        except Exception as exc:
            logger.warning("pin_chat_message failed for %s: %s", chat_id, exc)

    await deliver_attachments(
        bot,
        chat_id,
        attachments or [],
        caption_prefix=caption_prefix,
    )
    return sent_id


async def deliver_attachments(
    bot,
    chat_id: str,
    attachments: list[dict[str, Any]],
    *,
    caption_prefix: str | None = None,
) -> int:
    """Marks as text; readings/other as files."""
    sent = 0
    for att in attachments:
        intent = att.get("intent") or "other"
        filename = att.get("filename") or "attachment"
        extracted = (att.get("extracted_text") or "").strip()

        if intent == "marks" and extracted:
            try:
                await send_html(bot, chat_id, format_marks_for_telegram(filename, extracted))
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
    return sent


async def send_owner_update(
    bot,
    chat_id: str,
    text: str,
    *,
    assignment_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    pin: bool = False,
) -> None:
    if not chat_id:
        return
    if assignment_id:
        await send_actionable_alert(
            bot,
            chat_id,
            text,
            assignment_id=assignment_id,
            attachments=attachments,
            pin=pin,
        )
    else:
        await send_html(bot, chat_id, text)
        if attachments:
            await deliver_attachments(bot, chat_id, attachments)
