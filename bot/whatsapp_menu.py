"""WhatsApp-native main menu (interactive list message, MNP-bot style).

The Telegram bot uses a reply keyboard + long /start text. WhatsApp has no
reply keyboards, so instead we send one compact interactive *list* message:
the body carries the help text and every row maps to a ``cmd:<name>``
callback that the router turns back into a command dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

from services.messaging.whatsapp import get_whatsapp_client

logger = logging.getLogger(__name__)

# Callback id prefix for menu rows (handled in bot/whatsapp_router.py).
CMD_PREFIX = "cmd:"

# Texts that open the menu without a slash command.
MENU_KEYWORDS = {"menu", "main menu", "options"}

_WELCOME_BODY = (
    "👋 *Mailbot ready!*\n\n"
    "Morning (7 AM) and evening (8 PM) digests land here — "
    "if one is waiting, any reply opens it.\n"
    "Tap *📋 Open menu* below — or just type a question about "
    "your mail, deadlines, or tasks."
)

_HELP_BODY = (
    "*Mailbot commands* 🤖\n\n"
    "/due – upcoming deadlines\n"
    "/missed – overdue items\n"
    "/undated – tasks with no date\n"
    "/add – add a task\n"
    "/done <n> – finish item #n from last list\n"
    "/edit <n> – edit task #n\n"
    "/classes – today's classes\n"
    "/section – set your section\n"
    "/ask <question> – ask about mail/deadlines\n"
    "/send – compose an email\n"
    "/status – system health\n"
    "/cancel – stop the current flow\n"
    "/reset – clear chat context\n\n"
    "…or pick from the menu below 👇"
)

_MENU_BODY = "What do you need? Pick below 👇 or type a question."

# (callback suffix, row title ≤24 chars, row description ≤72 chars)
_MENU_ROWS: list[tuple[str, str, str]] = [
    ("due", "⏰ Due tasks", "Upcoming deadlines, nearest first"),
    ("missed", "⚠️ Missed", "Overdue and still open"),
    ("undated", "🗓️ Undated", "Tasks with no due date"),
    ("add", "➕ Add task", "Wizard or one-line add"),
    ("classes", "📚 Classes today", "Timetable for your section"),
    ("digest", "📰 Daily digest", "Morning/evening briefing"),
    ("send", "✉️ Send email", "Compose with AI help"),
    ("status", "📊 Bot status", "Health and last check times"),
    ("help", "❓ All commands", "Full command list"),
]

_BODIES = {
    "welcome": _WELCOME_BODY,
    "help": _HELP_BODY,
    "menu": _MENU_BODY,
}


def main_menu_interactive(style: str = "menu") -> dict[str, Any]:
    """Build the interactive list payload for the main menu."""
    body = _BODIES.get(style, _MENU_BODY)
    rows = [
        {
            "id": f"{CMD_PREFIX}{name}",
            "title": title,
            "description": desc,
        }
        for name, title, desc in _MENU_ROWS
    ]
    return {
        "type": "list",
        "body": {"text": body[:1024]},
        "action": {
            "button": "📋 Open menu",
            "sections": [{"title": "Mailbot menu", "rows": rows}],
        },
    }


async def send_main_menu(phone: str, *, style: str = "menu") -> bool:
    """Send the interactive main menu to the owner."""
    client = get_whatsapp_client()
    ok = await client.send_interactive(phone, main_menu_interactive(style))
    if not ok:
        # Fall back to plain text so the user still gets something useful.
        logger.warning("WhatsApp menu send failed (style=%s); falling back to text", style)
        fallback = _BODIES.get(style, _MENU_BODY) + "\n\nType /help anytime for commands."
        ok = await client.send_text(phone, fallback, from_html=False)
    return ok
