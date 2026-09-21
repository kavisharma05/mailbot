"""Orchestration for the IMT Access feature: catalog sync, analysis, daily digest.

The daily job is idempotent per IST calendar day (imt_state.last_daily_run_on)
so the in-app scheduler and the GitHub Actions backup cron cannot double-send.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from imt_access import analyze, delivery, store, stories
from imt_access.catalog import sync_catalog
from imt_access.portal import PortalClient
from imt_access.publications import (
    fetch_epw_current_issue,
    fetch_et_prime_stories,
    fetch_ft_headlines,
)
from imt_access.settings import IMTSettings, get_imt_settings

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def _sync_catalog_blocking(settings: IMTSettings) -> dict[str, Any]:
    with PortalClient(settings) as portal:
        portal.ensure_session()
        html = portal.get_resources_html()
    diff = sync_catalog(html)
    return {
        "total": diff.total,
        "new": [r.name for r in diff.new],
        "removed": [r.get("name") for r in diff.removed],
        "_new_resources": diff.new,
    }


async def _catalog_sync_internal(
    *, bot: Any = None, alert_new: bool = True
) -> tuple[dict[str, Any], list]:
    settings = get_imt_settings()
    if not settings.configured:
        return {"ok": False, "error": "not_configured"}, []
    try:
        result = await asyncio.to_thread(_sync_catalog_blocking, settings)
    except Exception as exc:
        logger.exception("Catalog sync failed: %s", exc)
        store.update_imt_state(last_error=f"catalog_sync: {exc}")
        return {"ok": False, "error": str(exc)}, []

    new_resources = result.pop("_new_resources", [])
    if alert_new and new_resources and bot is not None:
        try:
            await delivery.deliver_new_resources_alert(bot, new_resources)
        except Exception as exc:
            logger.warning("New-resources alert failed: %s", exc)
    return {"ok": True, **result}, new_resources


async def run_catalog_sync(*, alert_new: bool = True, bot: Any = None) -> dict[str, Any]:
    """Fetch /resources and upsert the catalog; optionally alert on new resources."""
    result, _new = await _catalog_sync_internal(bot=bot, alert_new=alert_new)
    return result


def _fetch_daily_blocking(settings: IMTSettings) -> dict[str, Any]:
    """Blocking part of the daily job: portal + external fetches."""
    out: dict[str, Any] = {"epw_issue": None, "ft": [], "et_prime": []}

    with PortalClient(settings) as portal:
        portal.ensure_session()
        try:
            issue = fetch_epw_current_issue(portal)
            out["epw_issue"] = issue
        except Exception as exc:
            logger.exception("EPW fetch failed: %s", exc)
            out["epw_error"] = str(exc)

    # External sources don't need the portal session.
    try:
        out["ft"] = fetch_ft_headlines(limit=settings.imt_headlines_per_source)
    except Exception as exc:
        logger.exception("FT fetch failed: %s", exc)
        out["ft_error"] = str(exc)
    try:
        out["et_prime"] = fetch_et_prime_stories(limit=settings.imt_headlines_per_source)
    except Exception as exc:
        logger.exception("ET Prime fetch failed: %s", exc)
        out["et_prime_error"] = str(exc)
    return out


async def run_daily_job(
    bot: Any,
    *,
    dry_run: bool = False,
    force: bool = False,
    include_catalog_sync: bool = True,
) -> dict[str, Any]:
    """Fetch publications and deliver the daily briefing. Idempotent per IST day."""
    settings = get_imt_settings()
    today = datetime.now(IST).date()

    if not settings.imt_daily_enabled:
        return {"ok": False, "skipped": "disabled", "date_ist": today.isoformat()}
    if not settings.configured:
        return {"ok": False, "skipped": "not_configured", "date_ist": today.isoformat()}
    if not dry_run and not force and store.daily_already_run(today):
        return {"ok": True, "skipped": "already_sent", "date_ist": today.isoformat()}

    logger.info("IMT daily job starting (dry_run=%s force=%s)", dry_run, force)

    new_resources: list = []
    catalog_result: dict[str, Any] = {}
    if include_catalog_sync:
        # Surface new resources inside the digest instead of a separate alert.
        catalog_result, new_resources = await _catalog_sync_internal(bot=bot, alert_new=False)

    fetched = await asyncio.to_thread(_fetch_daily_blocking, settings)
    epw_issue = fetched.get("epw_issue")
    ft_items = fetched.get("ft") or []
    et_items = fetched.get("et_prime") or []

    intro = None
    if settings.imt_llm_summaries:
        try:
            intro = await asyncio.to_thread(
                analyze.summarize_daily,
                epw_label=epw_issue.label if epw_issue else None,
                epw_count=len(epw_issue.items) if epw_issue else 0,
                ft_titles=[i.title for i in ft_items],
                et_titles=[i.title for i in et_items],
            )
        except Exception as exc:
            logger.info("LLM intro skipped: %s", exc)

    messages = delivery.build_digest_messages(
        epw_issue=epw_issue,
        ft_items=ft_items,
        et_items=et_items,
        intro=intro,
        new_resources=new_resources or None,
        run_date=today,
    )

    # Persist publication items for the web app + the story manifest so
    # /story <n> (or a bare "7" reply) resolves hours later on any channel.
    rows = []
    if epw_issue:
        rows.extend(item.to_row() for item in epw_issue.items)
    rows.extend(item.to_row() for item in ft_items)
    rows.extend(item.to_row() for item in et_items)
    inserted = await asyncio.to_thread(store.insert_publications, rows)
    await asyncio.to_thread(stories.save_manifest, messages["items"], today)

    result: dict[str, Any] = {
        "ok": True,
        "date_ist": today.isoformat(),
        "dry_run": dry_run,
        "epw_articles": len(epw_issue.items) if epw_issue else 0,
        "epw_issue": epw_issue.label if epw_issue else None,
        "ft_headlines": len(ft_items),
        "et_prime_stories": len(et_items),
        "stored_publications": inserted,
        "catalog": catalog_result or None,
        "errors": {
            k: v for k, v in fetched.items() if k.endswith("_error")
        },
    }

    if dry_run:
        result["preview"] = (messages["intro"] + "\n\n" + messages["stories"])[:2000]
        return result

    delivery_result = await delivery.deliver_daily_digest(bot, messages)
    result["delivery"] = delivery_result

    any_sent = bool(delivery_result.get("telegram") or delivery_result.get("whatsapp"))
    if any_sent:
        store.mark_daily_run(today)
    else:
        store.mark_daily_run(today, error="delivery_failed_on_all_channels")
        result["ok"] = False
        result["error"] = "delivery_failed"
    logger.info("IMT daily job done: %s", result)
    return result


def _analysis_blocking(settings: IMTSettings) -> dict[str, Any]:
    with PortalClient(settings) as portal:
        portal.ensure_session()
        html = portal.get_resources_html()
    from imt_access.catalog import parse_catalog

    resources = parse_catalog(html)
    report = analyze.build_catalog_analysis(resources)
    return {"resources": len(resources), "report": report}


async def run_analysis(*, save: bool = True) -> dict[str, Any]:
    """Generate (and store) the catalog analysis report."""
    settings = get_imt_settings()
    if not settings.configured:
        return {"ok": False, "error": "not_configured"}
    try:
        result = await asyncio.to_thread(_analysis_blocking, settings)
    except Exception as exc:
        logger.exception("Analysis failed: %s", exc)
        return {"ok": False, "error": str(exc)}
    report = result.get("report")
    if report and save:
        await asyncio.to_thread(store.save_report, "catalog_analysis", report)
    return {"ok": bool(report), "resources": result["resources"], "report": report}
