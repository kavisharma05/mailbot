"""Dispatch WhatsApp webhook events to existing Telegram handlers."""

from __future__ import annotations

import copy
import asyncio
import logging

from bot import handlers
from bot.handlers import _get_compose
from bot.whatsapp_adapter import (
    WhatsAppContext,
    build_callback_update,
    build_text_update,
    owner_phone_for_bot,
    whatsapp_bot,
)
from bot.whatsapp_menu import CMD_PREFIX, MENU_KEYWORDS, send_main_menu
from services.messaging.pending_digest import DIGEST_KEYWORDS, flush_pending_digest
from services.messaging.phone import normalize_wa_digits, wa_phones_match
from services.messaging.state import load_user_data, save_user_data
from services.messaging.whatsapp import get_whatsapp_client
from services.media_ai import analyze_image, transcribe_audio

logger = logging.getLogger(__name__)

_COMMANDS: dict[str, object] = {
    "start": handlers.start_command,
    "help": handlers.help_command,
    "status": handlers.status_command,
    "due": handlers.due_command,
    "list": handlers.due_command,
    "missed": handlers.missed_command,
    "undated": handlers.undated_command,
    "add": handlers.add_command,
    "edit": handlers.edit_command,
    "done": handlers.done_command,
    "classes": handlers.classes_command,
    "section": handlers.section_command,
    "library": handlers.library_command,
    "story": handlers.story_command,
    "ask": handlers.ask_command,
    "send": handlers.send_command,
    "cancel": handlers.cancel_command,
    "reset": handlers.reset_command,
    "new": handlers.reset_command,
    "backfill_contacts": handlers.backfill_contacts_command,
}

# Commands that show the WhatsApp-native interactive menu instead of the
# Telegram-oriented text (start/help share the same handler on Telegram).
_MENU_COMMAND_STYLES = {"start": "welcome", "help": "help", "menu": "menu"}
_SAFE_DURING_COMPOSE = {"cancel", "reset", "new", "send", "help", "menu", "start"}


async def _remind_compose_in_progress(phone: str) -> None:
    await get_whatsapp_client().send_text(
        phone,
        "You have an email in progress. Keep going, or type cancel to drop the draft.",
        from_html=False,
        template_fallback=False,
    )


async def _dispatch_callback(data: str, phone: str, user_data: dict) -> None:
    # Main-menu rows carry "cmd:<name>" — dispatch as if the user typed /<name>.
    if data.startswith(CMD_PREFIX):
        cmd = data[len(CMD_PREFIX):].strip().lower()
        if _get_compose(user_data) and cmd not in _SAFE_DURING_COMPOSE:
            await _remind_compose_in_progress(phone)
            return
        handler = _COMMANDS.get(cmd)
        if handler:
            if cmd in _MENU_COMMAND_STYLES:
                await send_main_menu(phone, style=_MENU_COMMAND_STYLES[cmd])
                return
            update = build_text_update(phone, f"/{cmd}", "")
            context = WhatsAppContext(bot=whatsapp_bot, user_data=user_data)
            await handler(update, context)  # type: ignore[operator]
        else:
            logger.info("Unknown WhatsApp menu command: %s", cmd[:40])
        return

    update = build_callback_update(phone, data)
    context = WhatsAppContext(bot=whatsapp_bot, user_data=user_data)

    if data.startswith("section:"):
        await handlers.section_button(update, context)
    elif data.startswith("story:"):
        await handlers.story_button(update, context)
    elif data.startswith("storylist:"):
        # Meta caps list messages at 10 rows, so the daily story picker drills
        # down one chunk at a time (rows here carry story:<n> onwards).
        from imt_access import stories as imt_stories
        from imt_access.delivery import whatsapp_story_chunk_picker

        try:
            offset = int(data.split(":", 1)[1])
        except ValueError:
            return
        manifest = imt_stories.load_manifest()
        items = manifest["items"] if manifest else []
        picker = whatsapp_story_chunk_picker(items, offset)
        if picker:
            await get_whatsapp_client().send_interactive(phone, picker)
    elif data == handlers.CLEAR_CHAT_CB:
        await handlers.clear_chat_button(update, context)
    elif data.startswith("act:"):
        await handlers.action_button(update, context)
    elif data.startswith(f"{handlers.ADDTASK_CB}:"):
        await handlers.addtask_callback(update, context)
    elif data.startswith(f"{handlers.EDITTASK_CB}:"):
        await handlers.edittask_callback(update, context)
    elif data in (handlers.APPROVE_CB, handlers.CANCEL_CB):
        await handlers.confirm_callback(update, context)
    else:
        logger.info("Unhandled WhatsApp callback: %s", data[:80])
        return


async def _dispatch_text(text: str, phone: str, message_id: str, user_data: dict) -> None:
    stripped = (text or "").strip()
    context = WhatsAppContext(bot=whatsapp_bot, user_data=user_data)

    if stripped.startswith("/"):
        parts = stripped.split()
        cmd = parts[0][1:].split("@")[0].lower()
        context.args = parts[1:]
        if _get_compose(user_data) and cmd not in _SAFE_DURING_COMPOSE:
            await _remind_compose_in_progress(phone)
            return
        if cmd in _MENU_COMMAND_STYLES:
            await send_main_menu(phone, style=_MENU_COMMAND_STYLES[cmd])
            return
        handler = _COMMANDS.get(cmd)
        if handler:
            update = build_text_update(phone, stripped, message_id)
            await handler(update, context)  # type: ignore[operator]
            return

    # Bare "menu" keyword (MNP-bot style) opens the interactive menu.
    if stripped.lower() in MENU_KEYWORDS:
        await send_main_menu(phone, style="menu")
        return

    # Quick-action labels (same as Telegram reply keyboard)
    if stripped in handlers.QUICK_ACTION_LABELS:
        if _get_compose(user_data) and stripped != "✉️ Send":
            await _remind_compose_in_progress(phone)
            return
        update = build_text_update(phone, stripped, message_id)
        await handlers.quick_action_message(update, context)
        return

    update = build_text_update(phone, stripped, message_id)
    await handlers.chat_message(update, context)


async def _process_media(
    message: dict,
    *,
    phone: str,
    message_id: str,
    user_data: dict,
) -> None:
    """Download and understand an inbound WhatsApp image or audio message."""
    msg_type = str(message.get("type") or "")
    media_info = message.get(msg_type) or {}
    media_id = str(media_info.get("id") or "")
    client = get_whatsapp_client()
    await client.send_text(
        phone,
        "Processing the image…" if msg_type == "image" else "Transcribing the audio…",
        from_html=False,
        template_fallback=False,
    )
    downloaded = await client.download_media(media_id)
    if not downloaded:
        await client.send_text(
            phone,
            "I couldn't download that media. Please send it again.",
            from_html=False,
            template_fallback=False,
        )
        return
    data, downloaded_mime = downloaded
    mime_type = str(media_info.get("mime_type") or downloaded_mime)
    if msg_type == "image":
        result = await asyncio.to_thread(
            analyze_image,
            data,
            mime_type=mime_type,
            caption=str(media_info.get("caption") or ""),
        )
        await client.send_text(
            phone,
            result
            or "I couldn't read that image accurately. Please try a clearer image.",
            from_html=False,
            template_fallback=False,
        )
        return

    extension = "ogg" if "ogg" in mime_type else "mp4" if "mp4" in mime_type else "mp3"
    transcript = await asyncio.to_thread(
        transcribe_audio,
        data,
        filename=f"voice.{extension}",
        mime_type=mime_type,
    )
    if not transcript:
        await client.send_text(
            phone,
            "I couldn't transcribe that audio accurately. Please try again with "
            "less background noise.",
            from_html=False,
            template_fallback=False,
        )
        return
    # Route the spoken request through the existing bot and reply in text.
    await _dispatch_text(transcript, phone, message_id, user_data)


async def process_whatsapp_message(phone: str, message: dict) -> None:
    """Handle one inbound WhatsApp message dict from Meta webhook payload."""
    owner = owner_phone_for_bot()
    normalized = normalize_wa_digits(phone)
    if owner and not wa_phones_match(normalized, owner):
        logger.info(
            "Ignoring WhatsApp message from non-owner %s (WHATSAPP_OWNER_PHONE=%s)",
            normalized,
            owner,
        )
        return

    logger.info("WhatsApp inbound from %s type=%s", normalized, message.get("type"))

    msg_id = str(message.get("id") or "")
    msg_type = message.get("type")

    # Any inbound message opens Meta's 24h window — deliver a stashed digest first.
    user_data = await load_user_data(normalized)
    snapshot = copy.deepcopy(user_data)
    flushed = await flush_pending_digest(normalized, user_data)

    # Menu / digest-keyword turns can return after the flush.
    if msg_type == "text":
        body = (message.get("text") or {}).get("body") or ""
        stripped = body.strip()
        lower = stripped.lower()
        if stripped.startswith("/"):
            cmd = stripped.split()[0][1:].split("@")[0].lower()
            if cmd in _MENU_COMMAND_STYLES:
                await send_main_menu(normalized, style=_MENU_COMMAND_STYLES[cmd])
                if user_data != snapshot:
                    await save_user_data(normalized, user_data)
                return
        if lower in MENU_KEYWORDS:
            await send_main_menu(normalized, style="menu")
            if user_data != snapshot:
                await save_user_data(normalized, user_data)
            return
        if lower in DIGEST_KEYWORDS:
            if not flushed:
                await get_whatsapp_client().send_text(
                    normalized,
                    "No stored digest right now. Morning (7 AM) and evening (8 PM) land here automatically.",
                    from_html=False,
                    template_fallback=False,
                )
            if user_data != snapshot:
                await save_user_data(normalized, user_data)
            return
    elif msg_type == "interactive":
        interactive = message.get("interactive") or {}
        if interactive.get("type") == "list_reply":
            cb = (interactive.get("list_reply") or {}).get("id") or ""
        elif interactive.get("type") == "button_reply":
            cb = (interactive.get("button_reply") or {}).get("id") or ""
        else:
            cb = ""
        if cb.startswith(CMD_PREFIX):
            cmd = cb[len(CMD_PREFIX):].strip().lower()
            if cmd in _MENU_COMMAND_STYLES:
                await send_main_menu(normalized, style=_MENU_COMMAND_STYLES[cmd])
                if user_data != snapshot:
                    await save_user_data(normalized, user_data)
                return
            if cmd == "digest":
                if not flushed:
                    await get_whatsapp_client().send_text(
                        normalized,
                        "No stored digest right now. Morning (7 AM) and evening (8 PM) land here automatically.",
                        from_html=False,
                        template_fallback=False,
                    )
                if user_data != snapshot:
                    await save_user_data(normalized, user_data)
                return

    try:
        if msg_type == "text":
            body = (message.get("text") or {}).get("body") or ""
            await _dispatch_text(body, normalized, msg_id, user_data)
        elif msg_type == "interactive":
            interactive = message.get("interactive") or {}
            if interactive.get("type") == "button_reply":
                cb = (interactive.get("button_reply") or {}).get("id") or ""
            elif interactive.get("type") == "list_reply":
                cb = (interactive.get("list_reply") or {}).get("id") or ""
            else:
                cb = ""
            if cb:
                await _dispatch_callback(cb, normalized, user_data)
        elif msg_type in {"image", "audio", "voice"}:
            await _process_media(
                message,
                phone=normalized,
                message_id=msg_id,
                user_data=user_data,
            )
        else:
            logger.debug("Unsupported WhatsApp message type: %s", msg_type)
    finally:
        if user_data != snapshot:
            await save_user_data(normalized, user_data)
