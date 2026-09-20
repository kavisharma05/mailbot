"""Delivery of IMT Access output: Telegram owner DM + WhatsApp + web storage.

No email is ever sent — Gmail is only used to READ OTP codes as a fallback.
"""

from __future__ import annotations

import io
import logging
from typing import Any

from config import get_settings
from services.formatting import escape_html
from services.messaging.whatsapp import get_whatsapp_client
from services.telegram_util import send_html_detailed

from imt_access.catalog import Resource
from imt_access.publications import EpwIssue, PdfAttachment, PublicationItem

logger = logging.getLogger(__name__)

_KIND_META = {
    "epw": ("📚", "EPW — Economic & Political Weekly"),
    "ft": ("🗞", "Financial Times — top headlines"),
    "et_prime": ("🇮🇳", "ET Prime — top stories"),
}


def _whatsapp_phone() -> str:
    return (get_settings().whatsapp_owner_phone or "").lstrip("+").replace(" ", "").replace("-", "")


def format_daily_digest(
    *,
    epw_issue: EpwIssue | None,
    ft_items: list[PublicationItem],
    et_items: list[PublicationItem],
    intro: str | None = None,
    new_resources: list[Resource] | None = None,
) -> str:
    """Build the Telegram-HTML daily digest."""
    lines: list[str] = ["<b>📖 Library Daily — e-resources briefing</b>", ""]
    if intro:
        lines.append(f"<i>{escape_html(intro.strip())}</i>")
        lines.append("")

    if epw_issue:
        lines.append(f"<b>{_KIND_META['epw'][0]} {escape_html(epw_issue.label)}</b>")
        for section, items in epw_issue.sections.items():
            if not items:
                continue
            lines.append(f"<u>{escape_html(section)}</u>")
            for item in items[:10]:
                lines.append(f"• {escape_html(item.title)}")
            if len(items) > 10:
                lines.append(f"  …and {len(items) - 10} more")
        lines.append("")

    for kind, items in (("ft", ft_items), ("et_prime", et_items)):
        if not items:
            continue
        emoji, label = _KIND_META[kind]
        lines.append(f"<b>{emoji} {label}</b>")
        for item in items:
            lines.append(f'• <a href="{escape_html(item.url)}">{escape_html(item.title)}</a>')
        lines.append("")

    if new_resources:
        lines.append("<b>🆕 New e-resources on the portal</b>")
        for res in new_resources[:10]:
            lines.append(f"• {escape_html(res.name)} <i>({escape_html(res.category)})</i>")
        lines.append("")

    lines.append("All links open via the library portal. More in the web app → Library tab.")
    return "\n".join(lines).strip()


def format_new_resources_alert(new_resources: list[Resource]) -> str:
    lines = ["<b>🆕 New library e-resources detected</b>", ""]
    for res in new_resources[:15]:
        lines.append(f"• <b>{escape_html(res.name)}</b> — {escape_html(res.category)}")
    lines.append("")
    lines.append("Open the web app → Library tab for the full catalog.")
    return "\n".join(lines)


async def _send_telegram(bot, chat_id: str, text: str) -> bool:
    result = await send_html_detailed(bot, chat_id, text, disable_web_page_preview=True)
    return result.ok


async def _send_whatsapp(text: str) -> bool:
    client = get_whatsapp_client()
    phone = _whatsapp_phone()
    if not client.enabled or not phone:
        return False
    return await client.send_text(phone, text, from_html=True, outbox_kind="imt_daily")


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
    text: str,
    pdfs: list[PdfAttachment] | None = None,
) -> dict[str, Any]:
    """Send the daily digest to the owner on Telegram + WhatsApp."""
    settings = get_settings()
    owner = settings.telegram_owner_chat_id
    result: dict[str, Any] = {"telegram": False, "whatsapp": False, "pdfs": {}}

    if owner:
        result["telegram"] = await _send_telegram(bot, owner, text)
    else:
        logger.warning("TELEGRAM_OWNER_CHAT_ID not set — skipping Telegram digest")

    result["whatsapp"] = await _send_whatsapp(text)

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
