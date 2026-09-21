"""Daily publication fetchers: EPW current issue, FT headlines, ET Prime stories.

- EPW is read through the portal's authenticated proxy (full current-issue
  table of contents, plus per-article PDF downloads).
- Financial Times and ET Prime are shared-credential external sites; their
  public listing pages expose headlines/story titles, which are enough for a
  daily digest. curl_cffi (chrome impersonation) gets past basic bot walls.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from imt_access.portal import PortalClient

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

EPW_PROXY_BASE = "https://www-epw-in.imtnagpur.mapmyaccess.com"
FT_HOME = "https://www.ft.com/"
ET_PRIME_HOME = "https://economictimes.indiatimes.com/prime"

_ISSUE_LABEL_RE = re.compile(
    r"Vol\.?\s*([\d]+),\s*Issue\s*No\.?\s*([\d]+),\s*(.+)", re.IGNORECASE
)
_ARTICLE_HREF_RE = re.compile(r"^/journal/\d{4}/[\d-]+/[a-z0-9-]+/[a-z0-9-]+\.html$", re.I)
_ISSUE_HREF_RE = re.compile(r"^/journal/\d{4}/[\d-]+$")


@dataclass
class PublicationItem:
    kind: str  # epw | ft | et_prime
    title: str
    url: str
    section: str = ""
    publication_date: date | None = None
    summary: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "section": self.section or None,
            "title": self.title,
            "url": self.url,
            "publication_date": (self.publication_date or datetime.now(IST).date()).isoformat(),
            "summary": self.summary or None,
            "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        }


@dataclass
class EpwIssue:
    label: str
    issue_path: str
    publication_date: date | None
    sections: dict[str, list[PublicationItem]] = field(default_factory=dict)

    @property
    def items(self) -> list[PublicationItem]:
        out: list[PublicationItem] = []
        for section_items in self.sections.values():
            out.extend(section_items)
        return out


def _parse_issue_date(label: str) -> date | None:
    match = _ISSUE_LABEL_RE.search(label)
    if not match:
        return None
    raw = match.group(3).strip()
    for fmt in ("%d %b, %Y", "%d %B, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def fetch_epw_current_issue(portal: PortalClient) -> EpwIssue | None:
    """Fetch the current EPW issue TOC through the portal proxy."""
    home = portal.epw_get("/")
    if home.status_code != 200:
        logger.warning("EPW proxy home returned %s", home.status_code)
        return None
    soup = BeautifulSoup(home.text, "lxml")

    issue_path = None
    issue_label = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # normalize absolute proxy URLs to paths
        if href.startswith(EPW_PROXY_BASE):
            href = href[len(EPW_PROXY_BASE):]
        text = re.sub(r"\s+", " ", a.get_text(strip=True))
        if _ISSUE_HREF_RE.match(href) and _ISSUE_LABEL_RE.search(text):
            issue_path = href
            issue_label = text
            break
    if not issue_path:
        logger.warning("Could not find current EPW issue link on proxy home")
        return None

    resp = portal.epw_get(issue_path)
    if resp.status_code != 200:
        logger.warning("EPW issue page %s returned %s", issue_path, resp.status_code)
        return None
    issue_soup = BeautifulSoup(resp.text, "lxml")
    if not issue_label and issue_soup.title:
        issue_label = issue_soup.title.string or ""

    issue = EpwIssue(
        label=issue_label or issue_path,
        issue_path=issue_path,
        publication_date=_parse_issue_date(issue_label),
    )

    current_section = "Articles"
    seen_urls: set[str] = set()
    for el in issue_soup.find_all(["h2", "h3", "h4", "a"]):
        if el.name in ("h2", "h3", "h4"):
            heading = re.sub(r"\s+", " ", el.get_text(strip=True))
            inner = el.find("a")
            inner_href = (inner.get("href") or "") if inner else ""
            # Headings that wrap an article link are article titles — the inner
            # <a> is visited next by this same iterator and added as an item.
            wraps_article = bool(inner_href and _ARTICLE_HREF_RE.match(inner_href))
            if (
                not wraps_article
                and heading
                and len(heading) <= 60
                and heading.lower() not in {"status message"}
            ):
                current_section = heading
            continue
        href = el.get("href") or ""
        if href.startswith(EPW_PROXY_BASE):
            href = href[len(EPW_PROXY_BASE):]
        if not _ARTICLE_HREF_RE.match(href):
            continue
        title = re.sub(r"\s+", " ", el.get_text(strip=True))
        if not title or len(title) < 8:
            continue
        url = f"{EPW_PROXY_BASE}{href}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        issue.sections.setdefault(current_section, []).append(
            PublicationItem(
                kind="epw",
                title=title,
                url=url,
                section=current_section,
                publication_date=issue.publication_date,
            )
        )

    if not issue.items:
        logger.warning("EPW issue page %s parsed with no articles", issue_path)
        return None
    return issue


def _curl_get(url: str) -> str | None:
    try:
        from curl_cffi import requests as creq

        resp = creq.get(url, impersonate="chrome", timeout=30)
        if resp.status_code != 200:
            logger.warning("%s returned %s", url, resp.status_code)
            return None
        return resp.text
    except Exception as exc:
        logger.warning("curl_cffi fetch of %s failed: %s", url, exc)
        return None


def fetch_ft_headlines(*, limit: int = 8) -> list[PublicationItem]:
    html = _curl_get(FT_HOME)
    if html and "/content/" not in html:
        # Bot-wall interstitial or markup drift — retry once before giving up.
        logger.info("FT homepage returned no content links; retrying once")
        html = _curl_get(FT_HOME)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    today = datetime.now(IST).date()
    items: list[PublicationItem] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/content/" not in href:
            continue
        title = re.sub(r"\s+", " ", a.get_text(strip=True))
        if len(title) < 30 or title in seen:
            continue
        seen.add(title)
        url = href if href.startswith("http") else f"https://www.ft.com{href}"
        items.append(
            PublicationItem(kind="ft", title=title, url=url, publication_date=today)
        )
        if len(items) >= limit:
            break
    return items


def fetch_et_prime_stories(*, limit: int = 8) -> list[PublicationItem]:
    html = _curl_get(ET_PRIME_HOME)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    today = datetime.now(IST).date()
    items: list[PublicationItem] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "prime" not in href:
            continue
        title = re.sub(r"\s+", " ", a.get_text(strip=True))
        if len(title) < 40 or title in seen:
            continue
        seen.add(title)
        url = href if href.startswith("http") else f"https://economictimes.indiatimes.com{href}"
        items.append(
            PublicationItem(kind="et_prime", title=title, url=url, publication_date=today)
        )
        if len(items) >= limit:
            break
    return items
