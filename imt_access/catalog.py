"""Parse the MapMyAccess /resources catalog and diff it against the DB.

The page is server-rendered: category sections are `div.row.categoryN` blocks
whose mapping to names lives in the sidebar (`displayItem('N')` links). Each
resource is a `.card` with an `h6.text-dark` title, an `a.viewBtn` access URL
(portal proxy or direct), and an `i.favouriteIcon` element whose id is the
portal's stable resource id.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from imt_access import store

logger = logging.getLogger(__name__)

_DISPLAY_ITEM_RE = re.compile(r"displayItem\('(\d+)'")
_CATEGORY_ROW_RE = re.compile(r"^category(\d+)$")


@dataclass
class Resource:
    portal_id: str
    name: str
    category: str
    url: str
    proxied: bool = False

    def to_row(self, now_iso: str, *, first_seen_at: str | None = None) -> dict[str, Any]:
        return {
            "portal_id": self.portal_id,
            "name": self.name,
            "category": self.category,
            "url": self.url,
            "proxied": self.proxied,
            "is_active": True,
            "first_seen_at": first_seen_at or now_iso,
            "last_seen_at": now_iso,
        }


@dataclass
class CatalogDiff:
    resources: list[Resource] = field(default_factory=list)
    new: list[Resource] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0


def _clean_title(raw: str) -> str:
    return re.sub(r"\s+", " ", raw or "").strip()


def parse_catalog(html: str) -> list[Resource]:
    """Parse /resources HTML into Resource records."""
    soup = BeautifulSoup(html, "lxml")

    # Sidebar: displayItem('N') -> human category name
    categories: dict[str, str] = {}
    for a in soup.find_all("a", onclick=_DISPLAY_ITEM_RE):
        match = _DISPLAY_ITEM_RE.search(a.get("onclick") or "")
        if match:
            categories[match.group(1)] = _clean_title(a.get_text())

    resources: list[Resource] = []
    seen_ids: set[str] = set()

    for row in soup.select("div.row.category"):
        cat_num = ""
        for cls in row.get("class", []):
            m = _CATEGORY_ROW_RE.match(cls)
            if m:
                cat_num = m.group(1)
                break
        category = categories.get(cat_num, f"Category {cat_num}" if cat_num else "Other")

        for card in row.select("div.card"):
            title_el = card.select_one("h6.text-dark") or card.find(["h6", "h5", "h4"])
            link_el = card.select_one("a.viewBtn") or card.find("a", href=True)
            star_el = card.select_one("i.favouriteIcon")
            name = _clean_title(title_el.get_text()) if title_el else ""
            url = (link_el.get("href") or "").strip() if link_el else ""
            if not name or not url:
                continue
            portal_id = (star_el.get("id") or "").strip() if star_el else ""
            if not portal_id:
                portal_id = hashlib.sha1(f"{name}|{url}".encode()).hexdigest()[:24]
            if portal_id in seen_ids:
                continue
            seen_ids.add(portal_id)
            resources.append(
                Resource(
                    portal_id=portal_id,
                    name=name,
                    category=category,
                    url=url,
                    proxied="mapmyaccess.com" in url,
                )
            )

    # Fallback: some portals render a media-list instead of cards.
    if not resources:
        for a in soup.select("a.result-link"):
            body = a.select_one(".media-body")
            name = _clean_title(body.get_text()) if body else _clean_title(a.get_text())
            url = (a.get("href") or "").strip()
            if not name or not url:
                continue
            portal_id = hashlib.sha1(f"{name}|{url}".encode()).hexdigest()[:24]
            if portal_id in seen_ids:
                continue
            seen_ids.add(portal_id)
            resources.append(
                Resource(
                    portal_id=portal_id,
                    name=name,
                    category="Other",
                    url=url,
                    proxied="mapmyaccess.com" in url,
                )
            )

    return resources


def sync_catalog(html: str) -> CatalogDiff:
    """Parse, diff against DB, upsert. Returns the diff summary."""
    resources = parse_catalog(html)
    now_iso = datetime.now(timezone.utc).isoformat()
    known = store.get_known_resources()

    diff = CatalogDiff(resources=resources, total=len(resources))

    rows: list[dict[str, Any]] = []
    for res in resources:
        existing = known.get(res.portal_id)
        if existing is None:
            diff.new.append(res)
            rows.append(res.to_row(now_iso))
        else:
            rows.append(res.to_row(now_iso, first_seen_at=existing.get("first_seen_at")))

    current_ids = {r.portal_id for r in resources}
    for pid, row in known.items():
        if pid not in current_ids and row.get("is_active", True):
            diff.removed.append(row)

    store.upsert_resources(rows)
    if diff.removed:
        store.mark_resources_inactive([str(r["portal_id"]) for r in diff.removed])

    store.update_imt_state(last_catalog_sync_at=now_iso)
    logger.info(
        "Catalog sync: %d resources (%d new, %d removed)",
        diff.total,
        len(diff.new),
        len(diff.removed),
    )
    return diff
