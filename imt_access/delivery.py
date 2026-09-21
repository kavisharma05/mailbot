"""Delivery of IMT Access output: Telegram owner DM + WhatsApp + web storage.

No email is ever sent — Gmail is only used to READ OTP codes as a fallback.
"""

from __future__ import annotations

import io
import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import get_settings
from services.formatting import escape_html
from services.messaging.whatsapp import get_whatsapp_client
from services.telegram_util import send_html_detailed

from imt_access.catalog import Resource
from imt_access.publications import EpwIssue, PdfAttachment, PublicationItem
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
    intro_lines.append("Today's stories follow in the next message — reply with any number for the full brief.")

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
    story_lines.append("<i>Reply with a number (e.g. 7) or /story 7 for the full brief.</i>")

    # --- featured picks: one-tap buttons for the day's most useful reads ---
    featured: list[int] = []
    if epw_issue and epw_issue.items:
        special = next(
            (it for it in epw_issue.items if it.section.lower() == "special articles"),
            epw_issue.items[0],
        )
        featured.append(items.index(special) + 1)
    if ft_items:
        featured.append(items.index(ft_items[0]) + 1)
    if et_items:
        featured.append(items.index(et_items[0]) + 1)

    # --- optional message 3: new resources ---
    new_res_text: str | None = None
    if new_resources:
        new_res_text = format_new_resources_alert(new_resources)

    return {
        "intro": "\n".join(intro_lines).strip(),
        "stories": "\n".join(story_lines).strip(),
        "new_resources": new_res_text,
        "items": items,
        "featured": featured,
    }


def format_new_resources_alert(new_resources: list[Resource]) -> str:
    lines = ["<b>🆕 New library e-resources detected</b>", ""]
    for res in new_resources[:15]:
        lines.append(f"• <b>{escape_html(res.name)}</b> — {escape_html(res.category)}")
    lines.append("")
    lines.append("Open the web app → Library tab for the full catalog.")
    return "\n".join(lines)


def _featured_keyboard(featured: list[int], items: list[PublicationItem]):
    """One-tap brief buttons for the day's featured stories (Telegram inline;
    auto-converts to WhatsApp reply buttons in send_text)."""
    if not featured:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = []
    for n in featured[:3]:
        item = items[n - 1] if 0 < n <= len(items) else None
        kind = str(getattr(item, "kind", "") or "")
        label = KIND_LABELS.get(kind, "Story")
        buttons.append(InlineKeyboardButton(f"⭐ #{n} {label}", callback_data=f"story:{n}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


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


async def _send_pdfs(bot, chat_id: str, pdfs: list[PdfAttachment]) -> dict[str, int]:
    """Send PDF attachments on Telegram and WhatsApp."""
    sent = {"telegram": 0, "whatsapp": 0}
    client = get_whatsapp_client()
    phone = _whatsapp_phone()
    for pdf in pdfs:
        try:
            from telegram import InputFile

            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(pdf.data), filename=pdf.filename),
                caption=pdf.caption[:1000],
            )
            sent["telegram"] += 1
        except Exception as exc:
            logger.warning("Telegram PDF send failed (%s): %s", pdf.filename, exc)
        if client.enabled and phone:
            try:
                if await client.send_document(
                    phone, pdf.data, pdf.filename, caption=pdf.caption[:1000], mime_type="application/pdf"
                ):
                    sent["whatsapp"] += 1
            except Exception as exc:
                logger.warning("WhatsApp PDF send failed (%s): %s", pdf.filename, exc)
    return sent


async def deliver_daily_digest(
    bot,
    messages: dict[str, Any],
    pdfs: list[PdfAttachment] | None = None,
) -> dict[str, Any]:
    """Send the daily briefing as separate messages on Telegram + WhatsApp.

    Order: intro -> numbered story list (with featured buttons) -> optional
    new-resources alert -> PDF attachments.
    """
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    result: dict[str, Any] = {"telegram": False, "whatsapp": False, "pdfs": {}}

    intro_text = str(messages.get("intro") or "")
    stories_text = str(messages.get("stories") or "")
    new_res_text = messages.get("new_resources")
    markup = _featured_keyboard(messages.get("featured") or [], messages.get("items") or [])

    if owner:
        ok_intro = await _send_telegram(bot, owner, intro_text)
        ok_stories = await _send_telegram(bot, owner, stories_text, reply_markup=markup)
        if new_res_text:
            await _send_telegram(bot, owner, str(new_res_text))
        result["telegram"] = ok_intro and ok_stories
    else:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping Telegram digest")

    ok_wa_intro = await _send_whatsapp(intro_text)
    ok_wa_stories = await _send_whatsapp(stories_text, reply_markup=markup)
    if new_res_text:
        await _send_whatsapp(str(new_res_text))
    result["whatsapp"] = ok_wa_intro and ok_wa_stories

    if pdfs and owner:
        result["pdfs"] = await _send_pdfs(bot, owner, pdfs)
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
