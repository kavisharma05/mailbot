"""Story detail on demand: digest manifest + full-story briefs.

The daily digest numbers every story (1..N). The ordered manifest is stored
in imt_reports (kind='daily_manifest') so /story <n>, tapping a featured
button, or simply replying with a number resolves the item hours later — on
Telegram, WhatsApp, or the web app. Generated briefs are cached in
imt_publications.summary so repeat requests are instant.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from imt_access import store
from imt_access.publications import EPW_PROXY_BASE, PublicationItem

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

MANIFEST_KIND = "daily_manifest"

KIND_LABELS = {"epw": "EPW", "ft": "Financial Times", "et_prime": "ET Prime"}

_PAYWALL_MARKERS = (
    "subscribe to read",
    "subscription required",
    "paywall",
    "register to continue",
    "sign in to read",
)


# ------------------------------------------------------------------ manifest


def save_manifest(items: list[PublicationItem], run_date: date) -> None:
    """Persist the ordered story list so later detail requests resolve."""
    payload = {
        "date": run_date.isoformat(),
        "items": [
            {
                "n": i + 1,
                "kind": it.kind,
                "title": it.title,
                "url": it.url,
                "section": it.section,
            }
            for i, it in enumerate(items)
        ],
    }
    store.save_report(MANIFEST_KIND, json.dumps(payload))


def load_manifest(*, max_age_days: int = 2) -> dict[str, Any] | None:
    """Load the most recent digest manifest (today or yesterday by default)."""
    row = store.get_latest_report(MANIFEST_KIND)
    if not row:
        return None
    try:
        data = json.loads(row.get("content") or "{}")
    except Exception:
        return None
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    try:
        manifest_date = date.fromisoformat(str(data.get("date") or ""))
    except ValueError:
        return None
    age = (datetime.now(IST).date() - manifest_date).days
    if age < 0 or age > max_age_days:
        return None
    return {"date": manifest_date.isoformat(), "items": items}


def manifest_item(manifest: dict[str, Any], n: int) -> dict[str, Any] | None:
    for item in manifest.get("items") or []:
        if item.get("n") == n:
            return item
    return None


def manifest_size(manifest: dict[str, Any] | None) -> int:
    return len((manifest or {}).get("items") or [])


# -------------------------------------------------------------- story fetch


def _extract_epw_body(html: str) -> str:
    """Full article text from an EPW article page (Drupal)."""
    soup = BeautifulSoup(html, "lxml")
    for selector in (".field-name-body", "article", ".node-content", ".region-content", "main"):
        node = soup.select_one(selector)
        if not node:
            continue
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if len(text) > 400:
            return text
    meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    return str(meta.get("content") or "").strip() if meta else ""


def _extract_web_body(html: str) -> str:
    """Best-effort text from FT / ET Prime (meta description + lead paragraphs)."""
    soup = BeautifulSoup(html, "lxml")
    meta = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    desc = str(meta.get("content") or "").strip() if meta else ""
    paras = []
    for p in soup.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(text) > 80:
            paras.append(text)
        if len(paras) >= 6:
            break
    return f"{desc}\n" + "\n".join(paras) if paras else desc


def fetch_story_text(item: dict[str, Any]) -> tuple[str, bool]:
    """Return (text, full_text_available). Paywalled sources yield a snippet."""
    kind = str(item.get("kind") or "")
    url = str(item.get("url") or "")
    if not url:
        return "", False

    if kind == "epw":
        try:
            from imt_access.portal import PortalClient

            path = url[len(EPW_PROXY_BASE):] if url.startswith(EPW_PROXY_BASE) else url
            with PortalClient() as portal:
                portal.ensure_session()
                resp = portal.epw_get(path)
            if resp.status_code == 200:
                text = _extract_epw_body(resp.text)
                if text:
                    return text, True
        except Exception as exc:
            logger.warning("EPW story fetch failed for %s: %s", url, exc)
        return "", False

    from imt_access.publications import _curl_get

    html = _curl_get(url)
    if not html:
        return "", False
    text = _extract_web_body(html)
    lowered = text.lower()
    full = len(text) > 600 and not any(m in lowered for m in _PAYWALL_MARKERS)
    return text, full


# -------------------------------------------------------------- story brief


def build_story_brief(item: dict[str, Any], text: str, full_text: bool) -> str | None:
    """LLM-written detailed brief. None when no LLM provider is available."""
    from ai.client import call_text

    title = str(item.get("title") or "")
    kind_label = KIND_LABELS.get(str(item.get("kind")), "News")
    if full_text and text:
        system = (
            "You brief an MBA student at IMT Nagpur on a news story. Write:\n"
            "- 3-5 bullet key points, each one crisp line starting with •\n"
            "- then one line 'Why it matters for you:' connecting the story to "
            "MBA coursework, placements, or business awareness.\n"
            "Plain text, no headers, no emojis, under 220 words. Be specific — "
            "names, numbers, and claims from the text, not generic filler."
        )
        prompt = f"Source: {kind_label}\nStory: {title}\n\nText:\n{text[:7000]}"
    else:
        system = (
            "You brief an MBA student at IMT Nagpur on a news story whose full "
            "text is behind a paywall. Based on the headline and snippet, write:\n"
            "- 2-4 bullet points on what the story is about and the context around it\n"
            "- then one line 'Why it matters for you:'\n"
            "Start with the exact line '(Full text is paywalled — brief based on "
            "the headline and available snippet.)'. Plain text, no headers, no "
            "emojis, under 160 words. Do not invent specific numbers or quotes."
        )
        prompt = f"Source: {kind_label}\nHeadline: {title}\n\nSnippet:\n{text[:1500]}"
    brief = call_text(system, prompt, max_tokens=420, temperature=0.4)
    return brief.strip() if brief else None


def get_brief_for_item(item: dict[str, Any], day: str) -> dict[str, Any]:
    """Cache -> fetch -> LLM brief -> cache, for one story item."""
    kind = str(item.get("kind") or "")
    url = str(item.get("url") or "")

    cached = store.get_publication_summary(kind, url, day)
    if cached:
        return {"item": item, "brief": cached, "cached": True, "date": day}

    text, full_text = fetch_story_text(item)
    brief = build_story_brief(item, text, full_text)
    if not brief and text:
        # LLM down but we have text — serve a plain excerpt.
        brief = text[:1200].strip()
    if not brief:
        return {"item": item, "brief": None, "cached": False, "date": day}

    store.update_publication_summary(kind, url, day, brief)
    return {"item": item, "brief": brief, "cached": False, "date": day}


def get_story_brief(n: int) -> dict[str, Any] | None:
    """Full flow: manifest lookup -> brief (used by chat channels)."""
    manifest = load_manifest()
    if not manifest:
        return None
    item = manifest_item(manifest, n)
    if not item:
        return None
    return get_brief_for_item(item, manifest["date"])


def format_story_brief_message(result: dict[str, Any]) -> str:
    """Telegram-HTML rendering of a story brief."""
    from services.formatting import escape_html

    item = result["item"]
    kind_label = KIND_LABELS.get(str(item.get("kind")), str(item.get("kind") or "News"))
    section = str(item.get("section") or "").strip()
    source_bits = " · ".join(b for b in (kind_label, section, str(result.get("date") or "")) if b)
    lines = [f"<b>📄 {escape_html(str(item.get('title') or ''))}</b>", ""]
    lines.append(f"<i>{escape_html(source_bits)}</i>")
    lines.append("")
    brief = result.get("brief")
    if brief:
        lines.append(escape_html(str(brief)))
        lines.append("")
    url = str(item.get("url") or "")
    if url:
        lines.append(f'<a href="{escape_html(url)}">Open the full story</a>')
    return "\n".join(lines).strip()
