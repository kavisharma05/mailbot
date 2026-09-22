"""Delivery of IMT Access output: Telegram owner DM + WhatsApp + web storage.

No email is ever sent — Gmail is only used to READ OTP codes as a fallback.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from services.formatting import escape_html
from services.messaging.whatsapp import get_whatsapp_client
from services.telegram_util import send_html_detailed

from imt_access.catalog import Resource
from imt_access.publications import EpwIssue, PublicationItem
from imt_access.stories import KIND_LABELS

IST = ZoneInfo("Asia/Kolkata")


logger = logging.getLogger(__name__)

_KIND_META = {
    "epw": ("📚", "EPW"),
    "ft": ("🗞", "Financial Times"),
    "et_prime": ("🇮🇳", "ET Prime"),
}

# Chat briefing stays short. The web Library tab still has the full catalog.
EPW_DIGEST_LIMIT = 2
HEADLINE_DIGEST_LIMIT = 2
_EPW_SKIP_SECTIONS = ("book review", "from 50 years", "letters", "archive")
_EPW_PREFER_SECTIONS = ("editorial", "commentary", "special article", "insight", "perspective")


def _whatsapp_phone() -> str:
    return (get_settings().whatsapp_owner_phone or "").lstrip("+").replace(" ", "").replace("-", "")


def _pick_epw(issue: EpwIssue | None, limit: int = EPW_DIGEST_LIMIT) -> list[PublicationItem]:
    """A couple of current EPW pieces, not book reviews or archive reprints."""
    if issue is None:
        return []
    preferred: list[PublicationItem] = []
    rest: list[PublicationItem] = []
    for section, section_items in issue.sections.items():
        low = section.lower()
        if any(skip in low for skip in _EPW_SKIP_SECTIONS):
            continue
        bucket = preferred if any(name in low for name in _EPW_PREFER_SECTIONS) else rest
        bucket.extend(section_items)
    return (preferred + rest)[:limit]


def _story_line(n: int, item: PublicationItem) -> str:
    title = escape_html(item.title)
    if item.url and item.kind != "epw":
        return f'{n}. <a href="{escape_html(item.url)}">{title}</a>'
    return f"{n}. {title}"


def build_digest_messages(
    *,
    epw_issue: EpwIssue | None,
    ft_items: list[PublicationItem],
    et_items: list[PublicationItem],
    intro: str | None = None,
    new_resources: list[Resource] | None = None,
    run_date: date | None = None,
) -> dict[str, Any]:
    """One short daily list: a few EPW pieces, two FT headlines, two ET stories.

    ``intro`` is ignored on purpose. The old editorial was a second wall of text
    that did not match the numbered list. The web Library tab keeps the full set.
    """
    del intro  # chat briefing no longer includes the long editorial
    day_label = (run_date or datetime.now(IST).date()).strftime("%a %d %b")
    groups: list[tuple[str, str, list[PublicationItem]]] = [
        ("epw", epw_issue.label if epw_issue else "EPW", _pick_epw(epw_issue)),
        ("ft", "", ft_items[:HEADLINE_DIGEST_LIMIT]),
        ("et_prime", "", et_items[:HEADLINE_DIGEST_LIMIT]),
    ]

    items: list[PublicationItem] = []
    lines = [
        f"<b>📖 Library — {day_label}</b>",
        "",
        "A few stories worth reading. Reply with a number for the brief.",
        "",
    ]
    n = 0
    for kind, extra_label, group_items in groups:
        if not group_items:
            continue
        emoji, label = _KIND_META[kind]
        heading = f"{emoji} {label}"
        if extra_label:
            heading += f" — {escape_html(extra_label)}"
        lines.append(f"<b>{heading}</b>")
        for item in group_items:
            n += 1
            items.append(item)
            lines.append(_story_line(n, item))
        lines.append("")
    if n == 0:
        lines.append("Nothing new from EPW, FT, or ET Prime today.")
    else:
        lines.append(f"<i>Reply 1–{n} for a detailed brief.</i>")

    new_res_text: str | None = None
    if new_resources:
        # Kept for the web/catalog path. The daily chat does not send this separately.
        new_res_text = format_new_resources_alert(new_resources[:3])

    text = "\n".join(lines).strip()
    return {
        "intro": "",
        "stories": text,
        "new_resources": new_res_text,
        "items": items,
    }


def format_new_resources_alert(new_resources: list[Resource]) -> str:
    lines = ["<b>🆕 New library e-resources detected</b>", ""]
    for res in new_resources[:15]:
        lines.append(f"• <b>{escape_html(res.name)}</b> — {escape_html(res.category)}")
    lines.append("")
    lines.append("Open the web app → Library tab for the full catalog.")
    return "\n".join(lines)


def _stories_keyboard(items: list[PublicationItem]):
    """Compact number grid — tap any story number to get its detailed brief."""
    if not items:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        InlineKeyboardButton(str(i), callback_data=f"story:{i}")
        for i in range(1, len(items) + 1)
    ]
    rows = [buttons[i : i + 6] for i in range(0, len(buttons), 6)]
    return InlineKeyboardMarkup(rows)


_WA_LIST_ROW_CAP = 10  # Meta enforces a 10-row total limit on list messages


def _item_get(item: Any, key: str) -> str:
    if isinstance(item, dict):
        return str(item.get(key) or "")
    return str(getattr(item, key, "") or "")


def _chunk_label(chunk: list[Any], start: int) -> str:
    kinds = {_item_get(it, "kind") for it in chunk}
    rng = f"{start + 1}–{start + len(chunk)}"
    if len(kinds) == 1:
        return f"{KIND_LABELS.get(next(iter(kinds)), 'Stories')} {rng}"[:24]
    return f"Stories {rng}"[:24]


def _whatsapp_story_picker(items: list[Any]) -> dict[str, Any] | None:
    """Top-level 'Read a story' list: one row per chunk of 10 stories.

    Meta caps list messages at 10 rows total, so 30+ stories can't be listed
    directly. Rows carry storylist:<offset> ids; the WhatsApp router answers
    with whatsapp_story_chunk_picker for that chunk (rows = story:<n>).
    """
    if not items:
        return None

    rows = []
    for start in range(0, len(items), _WA_LIST_ROW_CAP):
        chunk = items[start : start + _WA_LIST_ROW_CAP]
        rows.append({"id": f"storylist:{start}", "title": _chunk_label(chunk, start)})
    return {
        "type": "list",
        "body": {
            "text": "Reply with a story number, or pick a section."
        },
        "action": {"button": "📖 Read a story", "sections": [{"title": "Today's stories", "rows": rows}]},
    }


def whatsapp_story_chunk_picker(items: list[Any], offset: int) -> dict[str, Any] | None:
    """Second-level list: the 10 stories starting at <offset>, tappable."""
    if offset < 0:
        return None
    chunk = items[offset : offset + _WA_LIST_ROW_CAP]
    if not chunk:
        return None
    rows = [
        {
            "id": f"story:{offset + i + 1}",
            "title": f"{offset + i + 1}. {_item_get(it, 'title')}"[:24],
        }
        for i, it in enumerate(chunk)
    ]
    return {
        "type": "list",
        "body": {"text": "Tap a story for its detailed brief:"},
        "action": {
            "button": "📖 Stories",
            "sections": [{"title": _chunk_label(chunk, offset), "rows": rows}],
        },
    }


async def _send_whatsapp_picker(items: list[PublicationItem]) -> tuple[bool, str | None]:
    client = get_whatsapp_client()
    phone = _whatsapp_phone()
    picker = _whatsapp_story_picker(items)
    if not client.enabled or not phone or not picker:
        return False, "not_configured"
    try:
        return await client.send_interactive_detailed(phone, picker)
    except Exception as exc:
        # Outside the 24h window or list limits — the story text already tells
        # the user to reply with a number, so this is best-effort garnish.
        logger.info("WhatsApp story picker skipped: %s", exc)
        return False, str(exc)


def _peer_telegram_chats(owner_chat: str | None) -> list[str]:
    """Students with a Telegram chat. The library list is the same for everyone."""
    from services.db import multi_user_schema_ready

    if not multi_user_schema_ready():
        return []
    try:
        from services.users import is_owner_user, list_users
    except Exception:
        return []
    owner_chat = str(owner_chat or "")
    chats: list[str] = []
    seen = {owner_chat} if owner_chat else set()
    for user in list_users(active_only=True):
        if is_owner_user(user):
            continue
        chat = str(user.get("telegram_chat_id") or "").strip()
        if not chat or chat in seen:
            continue
        seen.add(chat)
        chats.append(chat)
    return chats


async def _send_telegram(bot, chat_id: str, text: str, reply_markup=None) -> bool:
    kwargs: dict[str, Any] = {"disable_web_page_preview": True}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    result = await send_html_detailed(bot, chat_id, text, **kwargs)
    return result.ok


async def _send_whatsapp(text: str, reply_markup=None) -> bool:
    client = get_whatsapp_client()
    phone = _whatsapp_phone()
    if not client.enabled or not phone:
        return False
    return await client.send_text(
        phone, text, from_html=True, outbox_kind="imt_daily", reply_markup=reply_markup
    )


async def deliver_daily_digest(
    bot,
    messages: dict[str, Any],
) -> dict[str, Any]:
    """Send the daily briefing on Telegram + WhatsApp: intro message, then the
    numbered story list with a tap-to-read picker (number grid on Telegram,
    interactive list on WhatsApp). No PDFs — details are on demand only.
    """
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    result: dict[str, Any] = {"telegram": False, "whatsapp": False}

    stories_text = str(messages.get("stories") or "")
    items: list[PublicationItem] = messages.get("items") or []
    markup = _stories_keyboard(items)

    if owner:
        result["telegram"] = await _send_telegram(bot, owner, stories_text, reply_markup=markup)
    else:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping Telegram digest")

    for chat_id in _peer_telegram_chats(owner):
        await _send_telegram(bot, chat_id, stories_text, reply_markup=markup)

    # One WhatsApp message. Replying with the number opens the brief; a second
    # "pick a section" list was a duplicate of this same list.
    result["whatsapp"] = await _send_whatsapp(stories_text)
    result["whatsapp_picker"] = False
    return result


async def deliver_new_resources_alert(bot, new_resources: list[Resource]) -> None:
    if not new_resources:
        return
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    text = format_new_resources_alert(new_resources)
    if owner:
        await _send_telegram(bot, owner, text)
    await _send_whatsapp(text)
