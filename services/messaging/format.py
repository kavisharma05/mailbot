"""Format Telegram-style HTML and keyboards for WhatsApp Cloud API."""

from __future__ import annotations

import re
from typing import Any

from services.telegram_util import chunk_text

# WhatsApp text body limit (Cloud API).
MAX_WA_BODY_CHARS = 4096
# Quick-reply button title limit.
MAX_BUTTON_TITLE = 20
# List row title limit.
MAX_LIST_ROW_TITLE = 24


def html_to_whatsapp(text: str) -> str:
    """Best-effort HTML → WhatsApp formatting (*bold*, _italic_, ```code```)."""
    if not text:
        return ""
    out = str(text)
    out = re.sub(r"<br\s*/?>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"</p>\s*<p>", "\n\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<p[^>]*>", "", out, flags=re.IGNORECASE)
    out = re.sub(r"</p>", "\n", out, flags=re.IGNORECASE)
    out = re.sub(r"<b[^>]*>(.*?)</b>", r"*\1*", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<strong[^>]*>(.*?)</strong>", r"*\1*", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<i[^>]*>(.*?)</i>", r"_\1_", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<em[^>]*>(.*?)</em>", r"_\1_", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<code[^>]*>(.*?)</code>", r"```\1```", out, flags=re.IGNORECASE | re.DOTALL)
    out = re.sub(r"<[^>]+>", "", out)
    out = out.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _truncate_title(title: str, limit: int) -> str:
    title = (title or "").strip()
    if len(title) <= limit:
        return title or "Option"
    return title[: limit - 1] + "…"


def _collect_inline_buttons(reply_markup: Any) -> list[tuple[str, str]]:
    """Return (callback_data, title) from PTB InlineKeyboardMarkup or compatible."""
    if reply_markup is None:
        return []
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return []
    buttons: list[tuple[str, str]] = []
    for row in keyboard:
        for btn in row:
            cb = getattr(btn, "callback_data", None) or ""
            title = getattr(btn, "text", None) or "Button"
            if cb:
                buttons.append((str(cb), str(title)))
    return buttons


def whatsapp_interactive_from_markup(
    reply_markup: Any,
    *,
    body_hint: str = "Choose an option",
) -> dict[str, Any] | None:
    """Build WhatsApp interactive payload (buttons or list) from inline keyboard."""
    buttons = _collect_inline_buttons(reply_markup)
    if not buttons:
        return None

    if len(buttons) <= 3:
        return {
            "type": "button",
            "body": {"text": _truncate_title(body_hint, 1024)},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": cb[:256],
                            "title": _truncate_title(title, MAX_BUTTON_TITLE),
                        },
                    }
                    for cb, title in buttons[:3]
                ]
            },
        }

    rows = [
        {
            "id": cb[:256],
            "title": _truncate_title(title, MAX_LIST_ROW_TITLE),
        }
        for cb, title in buttons[:10]
    ]
    return {
        "type": "list",
        "body": {"text": _truncate_title(body_hint, 1024)},
        "action": {
            "button": "Options",
            "sections": [{"title": "Actions", "rows": rows}],
        },
    }


def prepare_outbound_text(text: str, *, from_html: bool = True) -> list[str]:
    """Chunk text for WhatsApp delivery."""
    plain = html_to_whatsapp(text) if from_html else str(text or "").strip()
    return chunk_text(plain, limit=MAX_WA_BODY_CHARS - 50) or ([plain] if plain else [])
