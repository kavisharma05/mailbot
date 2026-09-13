"""Dispatch WhatsApp webhook events to existing Telegram handlers."""

from __future__ import annotations

import copy
import logging

from bot import handlers
from bot.whatsapp_adapter import (
    WhatsAppContext,
    build_callback_update,
    build_text_update,
    owner_phone_for_bot,
    whatsapp_bot,
)
from bot.whatsapp_menu import CMD_PREFIX, MENU_KEYWORDS, send_main_menu
from services.messaging.phone import normalize_wa_digits, wa_phones_match
from services.messaging.state import load_user_data, save_user_data

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


async def _dispatch_callback(data: str, phone: str, user_data: dict) -> None:
    # Main-menu rows carry "cmd:<name>" — dispatch as if the user typed /<name>.
    if data.startswith(CMD_PREFIX):
        cmd = data[len(CMD_PREFIX):].strip().lower()
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
        update = build_text_update(phone, stripped, message_id)
        await handlers.quick_action_message(update, context)
        return

    update = build_text_update(phone, stripped, message_id)
    await handlers.chat_message(update, context)


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

    # Menu-only turns never touch session state — skip the Supabase round-trip.
    if msg_type == "text":
        body = (message.get("text") or {}).get("body") or ""
        stripped = body.strip()
        if stripped.startswith("/"):
            cmd = stripped.split()[0][1:].split("@")[0].lower()
            if cmd in _MENU_COMMAND_STYLES:
                await send_main_menu(normalized, style=_MENU_COMMAND_STYLES[cmd])
                return
        if stripped.lower() in MENU_KEYWORDS:
            await send_main_menu(normalized, style="menu")
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
                return

    user_data = await load_user_data(normalized)
    snapshot = copy.deepcopy(user_data)

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
        else:
            logger.debug("Unsupported WhatsApp message type: %s", msg_type)
    finally:
        if user_data != snapshot:
            await save_user_data(normalized, user_data)
