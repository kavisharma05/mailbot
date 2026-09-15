"""WhatsApp Cloud API client (Meta Graph API)."""

from __future__ import annotations

import logging
import mimetypes
from functools import lru_cache
from typing import Any

import httpx

from config import Settings, get_settings
from services.messaging.format import (
    prepare_outbound_text,
    whatsapp_interactive_from_markup,
)

logger = logging.getLogger(__name__)

# Re-engagement / outside 24h customer service window (common subcodes vary).
_OUTSIDE_WINDOW_MARKERS = ("131047", "131026", "470", "outside the allowed window")

# Shared HTTP client: keep-alive pooling avoids a fresh TLS handshake to
# graph.facebook.com on every send (saves ~100-300ms per API call).
_shared_http: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.AsyncClient(
            timeout=httpx.Timeout(45.0),
            limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=300),
        )
    return _shared_http


class WhatsAppClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token = (settings.whatsapp_access_token or "").strip()
        self._phone_id = (settings.whatsapp_phone_number_id or "").strip()
        self._version = (settings.whatsapp_api_version or "v22.0").strip()

    @property
    def can_send(self) -> bool:
        """Token + phone number ID — enough to reply to inbound messages."""
        return bool(self._token and self._phone_id)

    @property
    def enabled(self) -> bool:
        """Owner alert mirroring (requires explicit opt-in)."""
        return bool(
            self.can_send
            and self._settings.whatsapp_enabled
            and self._settings.whatsapp_owner_phone
        )

    def _messages_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/{self._phone_id}/messages"

    def _media_url(self) -> str:
        return f"https://graph.facebook.com/{self._version}/{self._phone_id}/media"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _normalize_to(phone: str) -> str:
        return str(phone or "").lstrip("+").replace(" ", "").replace("-", "")

    def _is_outside_window_error(self, resp: httpx.Response) -> bool:
        try:
            body = resp.json()
        except Exception:
            body = {}
        err = body.get("error") or {}
        msg = str(err.get("message", "")) + str(err.get("error_subcode", "")) + str(err.get("code", ""))
        return any(marker in msg for marker in _OUTSIDE_WINDOW_MARKERS)

    async def mark_read(self, message_id: str, *, typing: bool = False) -> None:
        """Mark a message read; optionally show the 'typing…' indicator.

        The typing indicator stays visible until we reply (or ~25s pass),
        which makes LLM-backed answers feel much more responsive.
        """
        if not message_id or not self.can_send:
            return
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await _http().post(
                self._messages_url(), headers=self._headers(), json=payload, timeout=20
            )
        except Exception as exc:
            logger.debug("WhatsApp mark_read failed: %s", exc)

    async def send_typing(self, message_id: str) -> None:
        """Show 'typing…' for an inbound message (also marks it read)."""
        await self.mark_read(message_id, typing=True)

    async def _post_message(self, to: str, payload: dict[str, Any]) -> httpx.Response:
        body = {"messaging_product": "whatsapp", "to": self._normalize_to(to), **payload}
        return await _http().post(self._messages_url(), headers=self._headers(), json=body)

    async def send_template(
        self,
        to: str,
        template_name: str,
        *,
        language_code: str = "en_US",
        body_parameter: str | None = None,
    ) -> bool:
        if not template_name:
            return False
        template: dict[str, Any] = {
            "name": template_name,
            "language": {"code": language_code},
        }
        if body_parameter is not None:
            template["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": body_parameter[:1024]}],
                }
            ]
        resp = await self._post_message(to, {"type": "template", "template": template})
        if resp.status_code >= 400:
            logger.warning("WhatsApp template send failed %s: %s", resp.status_code, resp.text[:500])
            return False
        return True

    async def send_text(
        self,
        to: str,
        text: str,
        *,
        from_html: bool = True,
        reply_markup: Any = None,
        template_fallback: bool = True,
    ) -> bool:
        if not self.can_send or not text or not str(text).strip():
            if not self.can_send:
                logger.warning("WhatsApp send_text skipped: missing token or phone number ID")
            return False

        pieces = prepare_outbound_text(text, from_html=from_html)
        if not pieces:
            return False

        interactive = whatsapp_interactive_from_markup(reply_markup, body_hint=pieces[-1][:200])
        ok = True
        for i, piece in enumerate(pieces):
            is_last = i == len(pieces) - 1
            if is_last and interactive:
                payload: dict[str, Any] = {"type": "interactive", "interactive": interactive}
                if interactive.get("type") == "button":
                    payload["interactive"]["body"] = {"text": piece[:1024]}
                elif interactive.get("type") == "list":
                    payload["interactive"]["body"] = {"text": piece[:1024]}
                resp = await self._post_message(to, payload)
            else:
                resp = await self._post_message(to, {"type": "text", "text": {"body": piece}})

            if resp.status_code >= 400:
                logger.warning("WhatsApp text send failed %s: %s", resp.status_code, resp.text[:500])
                ok = False
                if template_fallback and self._is_outside_window_error(resp):
                    tmpl = (self._settings.whatsapp_alert_template or "").strip()
                    if tmpl:
                        summary = prepare_outbound_text(text, from_html=from_html)
                        fallback_body = summary[0] if summary else piece
                        return await self.send_template(to, tmpl, body_parameter=fallback_body[:1024])
                break
        return ok

    async def send_interactive(self, to: str, interactive: dict[str, Any]) -> bool:
        """Send a raw interactive payload (reply buttons or list message)."""
        if not self.can_send or not interactive:
            return False
        resp = await self._post_message(to, {"type": "interactive", "interactive": interactive})
        if resp.status_code >= 400:
            logger.warning("WhatsApp interactive send failed %s: %s", resp.status_code, resp.text[:500])
            return False
        return True

    async def send_document(
        self,
        to: str,
        data: bytes,
        filename: str,
        *,
        caption: str | None = None,
        mime_type: str | None = None,
    ) -> bool:
        if not self.enabled or not data:
            return False
        to_norm = self._normalize_to(to)
        content_type = (
            (mime_type or "").strip()
            or mimetypes.guess_type(filename or "")[0]
            or "application/octet-stream"
        )
        try:
            client = _http()
            upload = await client.post(
                self._media_url(),
                headers={"Authorization": f"Bearer {self._token}"},
                data={"messaging_product": "whatsapp", "type": content_type},
                files={"file": (filename or "attachment", data, content_type)},
                timeout=60,
            )
            if upload.status_code >= 400:
                logger.warning("WhatsApp media upload failed: %s", upload.text[:400])
                return False
            media_id = (upload.json() or {}).get("id")
            if not media_id:
                return False
            doc: dict[str, Any] = {"id": media_id, "filename": filename[:240]}
            payload: dict[str, Any] = {
                "type": "document",
                "document": doc,
            }
            if caption:
                payload["document"]["caption"] = caption[:1024]
            resp = await client.post(
                self._messages_url(),
                headers=self._headers(),
                json={
                    "messaging_product": "whatsapp",
                    "to": to_norm,
                    **payload,
                },
                timeout=60,
            )
            if resp.status_code >= 400:
                logger.warning("WhatsApp document send failed: %s", resp.text[:400])
                return False
            return True
        except Exception as exc:
            logger.warning("WhatsApp send_document error: %s", exc)
            return False


@lru_cache
def get_whatsapp_client() -> WhatsAppClient:
    return WhatsAppClient(get_settings())


def is_owner_telegram_chat(chat_id: str | int | None) -> bool:
    if chat_id is None:
        return False
    settings = get_settings()
    owner = (settings.telegram_owner_chat_id or "").strip()
    return owner != "" and str(chat_id) == owner
