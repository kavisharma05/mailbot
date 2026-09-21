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
    "epw": ("📚", "EPW — Economic & Political Weekly"),
    "ft": ("🗞", "Financial Times — top headlines"),
    "et_prime": ("🇮🇳", "ET Prime — top stories"),
}


def _whatsapp_phone() -> str:
    return (get_settings().whatsapp_owner_phone or "").lstrip("+").replace(" ", "").replace("-", "")


def build_digest_messages(
    *,
    epw_issue: EpwIssue | None,
    ft_items: list[PublicationItem],
    et_items: list[PublicationItem],
    intro: str | None = None,
    new_resources: list[Resource] | None = None,
    run_date: date | None = None,
) -> dict[str, Any]:
    """Build the daily briefing as separate, focused messages (less clutter).

    Returns:
      intro         — short header + LLM editorial (its own message)
      stories       — the numbered story list (its own message)
      new_resources — optional alert text when the portal adds databases
      items         — ordered story list (numbering matches the manifest)
      featured      — story numbers worth a one-tap button
    """
    day_label = (run_date or datetime.now(IST).date()).strftime("%a %d %b")

    items: list[PublicationItem] = []
    if epw_issue:
        items.extend(epw_issue.items)
    items.extend(ft_items)
    items.extend(et_items)

    # --- message 1: intro ---
    intro_lines = [f"<b>📖 Library Daily — {day_label}</b>", ""]
    if intro:
        intro_lines.append(f"<i>{escape_html(intro.strip())}</i>")
        intro_lines.append("")
    intro_lines.append("Today's stories follow — tap a number (or reply with it) for a detailed brief.")

    # --- message 2: numbered story list ---
    story_lines: list[str] = ["<b>🗞 Today's stories</b>", ""]
    n = 0
    if epw_issue:
        story_lines.append(f"<b>{_KIND_META['epw'][0]} {escape_html(epw_issue.label)}</b>")
        for section, section_items in epw_issue.sections.items():
            if not section_items:
                continue
            story_lines.append(f"<u>{escape_html(section)}</u>")
            for item in section_items:
                n += 1
                story_lines.append(f"{n}. {escape_html(item.title)}")
        story_lines.append("")
    for kind, kind_items in (("ft", ft_items), ("et_prime", et_items)):
        if not kind_items:
            continue
        emoji, label = _KIND_META[kind]
        story_lines.append(f"<b>{emoji} {label}</b>")
        for item in kind_items:
            n += 1
            story_lines.append(
                f'{n}. <a href="{escape_html(item.url)}">{escape_html(item.title)}</a>'
            )
        story_lines.append("")
    story_lines.append("<i>Which one do you want to read? Tap a number below 👇</i>")

    # --- optional message 3: new resources ---
    new_res_text: str | None = None
    if new_resources:
        new_res_text = format_new_resources_alert(new_resources)

    return {
        "intro": "\n".join(intro_lines).strip(),
        "stories": "\n".join(story_lines).strip(),
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
            "text": "Which story do you want to read in detail? "
            "Pick a section below — or just reply with its number."
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

    intro_text = str(messages.get("intro") or "")
    stories_text = str(messages.get("stories") or "")
    new_res_text = messages.get("new_resources")
    items: list[PublicationItem] = messages.get("items") or []
    markup = _stories_keyboard(items)

    if owner:
        ok_intro = await _send_telegram(bot, owner, intro_text)
        ok_stories = await _send_telegram(bot, owner, stories_text, reply_markup=markup)
        if new_res_text:
            await _send_telegram(bot, owner, str(new_res_text))
        result["telegram"] = ok_intro and ok_stories
    else:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping Telegram digest")

    ok_wa_intro = await _send_whatsapp(intro_text)
    ok_wa_stories = await _send_whatsapp(stories_text)
    picker_ok, picker_err = await _send_whatsapp_picker(items)
    result["whatsapp_picker"] = picker_ok
    if picker_err:
        result["whatsapp_picker_error"] = picker_err
    if new_res_text:
        await _send_whatsapp(str(new_res_text))
    result["whatsapp"] = ok_wa_intro and ok_wa_stories
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
