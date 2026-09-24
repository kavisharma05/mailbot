"""PTB-compatible shims so bot/handlers.py can run on WhatsApp Cloud API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from telegram.constants import ChatType

from services.messaging.format import html_to_whatsapp, whatsapp_interactive_from_markup
from services.messaging.phone import normalize_wa_digits, phone_to_stable_user_id
from services.messaging.whatsapp import get_whatsapp_client
from services.telegram_util import chunk_text

logger = logging.getLogger(__name__)


def _phone_to_user_id(phone: str) -> int:
    return phone_to_stable_user_id(phone)


def _whatsapp_actor_id(phone: str) -> int:
    """Map this WhatsApp chat to the user's Telegram id when possible."""
    try:
        from services.messaging.whatsapp_routing import whatsapp_actor_telegram_id

        return whatsapp_actor_telegram_id(phone)
    except Exception:
        logger.debug("WhatsApp actor id lookup failed", exc_info=True)
        return _phone_to_user_id(phone)


@dataclass
class WhatsAppUser:
    id: int
    first_name: str = "Owner"


@dataclass
class WhatsAppChat:
    id: int
    type: str = ChatType.PRIVATE
    # Inbound message id enables the Cloud API "typing…" indicator.
    inbound_message_id: str = ""

    async def send_action(self, action: str) -> None:
        if action != "typing" or not self.inbound_message_id:
            return
        try:
            await get_whatsapp_client().send_typing(self.inbound_message_id)
        except Exception as exc:
            logger.debug("WhatsApp typing indicator failed: %s", exc)


class WhatsAppMessage:
    def __init__(self, phone: str, text: str, message_id: str, bot: WhatsAppBot) -> None:
        self._phone = phone
        self.text = text
        self.message_id = message_id
        self._bot = bot
        phone_id = _phone_to_user_id(phone)
        self.from_user = WhatsAppUser(id=_whatsapp_actor_id(phone))
        self.chat = WhatsAppChat(id=phone_id, inbound_message_id=message_id)
        self.reply_to_message = None
        self.entities = None

    async def reply_text(self, text: str, parse_mode: str | None = None, reply_markup: Any = None, **kwargs: Any):
        del kwargs
        await self._bot.send_message(
            self._phone,
            text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        return _SentMessageStub()


class _SentMessageStub:
    message_id = 0


class WhatsAppCallbackMessage:
    def __init__(self, phone: str, bot: WhatsAppBot) -> None:
        self._phone = phone
        self._bot = bot
        self._query: WhatsAppCallbackQuery | None = None
        phone_id = _phone_to_user_id(phone)
        self.chat_id = phone_id
        self.chat = WhatsAppChat(id=phone_id)
        self.text: str | None = None
        self.caption: str | None = None
        self.message_id = 0
        self.reply_markup = None

    async def reply_text(self, text: str, parse_mode: str | None = None, reply_markup: Any = None, **kwargs: Any):
        del kwargs
        if self._query is not None:
            self._query._notice = None
            self._query._replied = True
        await self._bot.send_message(self._phone, text, parse_mode=parse_mode, reply_markup=reply_markup)
        return _SentMessageStub()

    async def delete(self) -> None:
        return None


class WhatsAppCallbackQuery:
    def __init__(self, phone: str, data: str, bot: WhatsAppBot) -> None:
        self._phone = phone
        self.data = data
        self._bot = bot
        self._notice: str | None = None
        self._replied = False
        self.message = WhatsAppCallbackMessage(phone, bot)
        self.message._query = self
        self.from_user = WhatsAppUser(id=_whatsapp_actor_id(phone))

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        """Telegram shows this as a toast. WhatsApp has no toast, so keep the text to send."""
        del show_alert
        if text and str(text).strip():
            self._notice = str(text).strip()

    def take_notice(self) -> str | None:
        text = None if self._replied else self._notice
        self._notice = None
        return text

    async def edit_message_text(
        self,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._notice = None
        self._replied = True
        await self._bot.send_message(self._phone, text, parse_mode=parse_mode, reply_markup=reply_markup)


class WhatsAppUpdate:
    is_whatsapp = True

    def __init__(
        self,
        *,
        message: WhatsAppMessage | None = None,
        callback_query: WhatsAppCallbackQuery | None = None,
        whatsapp_phone: str = "",
    ) -> None:
        self.message = message
        self.callback_query = callback_query
        self.whatsapp_phone = normalize_wa_digits(
            whatsapp_phone
            or (message._phone if message else "")
            or (callback_query._phone if callback_query else "")
        )
        if message:
            self.effective_user = message.from_user
            self.effective_chat = message.chat
        elif callback_query:
            self.effective_user = callback_query.from_user
            self.effective_chat = callback_query.message.chat
        else:
            self.effective_user = None
            self.effective_chat = None


@dataclass
class WhatsAppContext:
    bot: WhatsAppBot
    user_data: dict
    args: list[str] = field(default_factory=list)


class WhatsAppBot:
    """Minimal bot surface used by handlers and alert delivery."""

    username: str | None = None
    id: int = 0

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: str | None = "HTML",
        reply_markup: Any = None,
        **kwargs: Any,
    ):
        del kwargs
        phone = str(chat_id).lstrip("+")
        client = get_whatsapp_client()
        from_html = parse_mode == "HTML"
        ok = await client.send_text(phone, text, from_html=from_html, reply_markup=reply_markup)
        if not ok:
            raise RuntimeError("WhatsApp send failed")
        return _SentMessageStub()

    async def delete_message(self, chat_id: str | int, message_id: int) -> None:
        del chat_id, message_id

    async def unpin_chat_message(self, chat_id: str | int, message_id: int) -> None:
        del chat_id, message_id

    async def pin_chat_message(self, chat_id: str | int, message_id: int, disable_notification: bool = True) -> None:
        del chat_id, message_id, disable_notification

    async def send_document(self, chat_id: str | int, document: Any, caption: str | None = None, **kwargs: Any):
        del kwargs
        phone = str(chat_id)
        client = get_whatsapp_client()
        filename = getattr(document, "filename", None) or "attachment"
        data = None
        if hasattr(document, "read"):
            data = document.read()
        elif hasattr(document, "getvalue"):
            data = document.getvalue()
        if not data and isinstance(document, (bytes, bytearray)):
            data = bytes(document)
        if not data:
            return None
        await client.send_document(
            phone,
            data,
            str(filename),
            caption=caption,
            mime_type=getattr(document, "mime_type", None),
        )
        return _SentMessageStub()


# Shared bot instance for inbound + outbound owner fan-out.
whatsapp_bot = WhatsAppBot()


async def reply_long_whatsapp(message: WhatsAppMessage, text: str, parse_mode: str | None = None) -> bool:
    """Chunked reply for adapter paths that bypass telegram_util.reply_long."""
    plain = html_to_whatsapp(text) if parse_mode == "HTML" else str(text)
    pieces = chunk_text(plain)
    client = get_whatsapp_client()
    ok = True
    for piece in pieces:
        if not await client.send_text(message._phone, piece, from_html=False):
            ok = False
    return ok


def build_text_update(phone: str, text: str, message_id: str) -> WhatsAppUpdate:
    msg = WhatsAppMessage(phone, text, message_id, whatsapp_bot)
    return WhatsAppUpdate(message=msg)


def build_callback_update(phone: str, callback_data: str) -> WhatsAppUpdate:
    cb = WhatsAppCallbackQuery(phone, callback_data, whatsapp_bot)
    return WhatsAppUpdate(callback_query=cb)


def owner_phone_for_bot() -> str:
    from config import get_settings

    return (get_settings().whatsapp_owner_phone or "").lstrip("+").replace(" ", "")
