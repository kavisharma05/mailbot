"""LLM-powered analysis of the e-resources catalog.

Produces a markdown report (stored in imt_reports and shown on the web app):
what the library subscribes to, what each resource is for, and which ones
matter most for common MBA work (finance, marketing, ops, research, news).
"""

from __future__ import annotations

import logging
from typing import Any

from ai.client import call_text
from imt_access.catalog import Resource

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a librarian assistant for an MBA student at IMT Nagpur. "
    "Write a crisp, practical markdown briefing about the library's e-resource "
    "subscriptions. Be specific about what each resource is best used for. "
    "Keep it under 400 words. Use short sections with ## headers and bullets."
)

_PROMPT = """Here is the current e-resources catalog from the college library portal
(grouped by category):

{catalog}

Write a briefing with:
## What we have — one line per category with the count
## Heavy hitters — the 5-8 most useful resources for an MBA and exactly what to use them for
## Daily reading — which newspaper/magazine subscriptions are included
## Hidden gems — 2-3 resources students usually miss
"""


def _format_catalog(resources: list[Resource]) -> str:
    by_cat: dict[str, list[str]] = {}
    for res in resources:
        by_cat.setdefault(res.category, []).append(res.name)
    lines: list[str] = []
    for category in sorted(by_cat):
        lines.append(f"{category} ({len(by_cat[category])}):")
        for name in sorted(by_cat[category]):
            lines.append(f"- {name}")
    return "\n".join(lines)


def build_catalog_analysis(resources: list[Resource]) -> str | None:
    """Return a markdown analysis report, or None when no LLM is available."""
    if not resources:
        return None
    catalog_text = _format_catalog(resources)
    report = call_text(
        _SYSTEM,
        _PROMPT.format(catalog=catalog_text[:6000]),
        max_tokens=900,
        temperature=0.4,
    )
    if not report:
        logger.warning("Catalog analysis skipped — no LLM provider available")
        return None
    return report.strip()


def summarize_daily(
    *,
    epw_label: str | None,
    epw_count: int,
    ft_titles: list[str],
    et_titles: list[str],
) -> str | None:
    """A 2-3 line LLM intro for the daily digest. Optional garnish."""
    parts: list[str] = []
    if epw_label:
        parts.append(f"EPW {epw_label}: {epw_count} articles")
    if ft_titles:
        parts.append("FT: " + "; ".join(ft_titles[:5]))
    if et_titles:
        parts.append("ET Prime: " + "; ".join(et_titles[:5]))
    if not parts:
        return None
    return call_text(
        "You write one tight morning-brief intro for an MBA student. "
        "2-3 sentences, plain text, no emojis, no greeting. Mention the single "
        "most interesting item and why it matters.",
        "\n".join(parts)[:3000],
        max_tokens=140,
        temperature=0.5,
    )
