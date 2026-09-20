"""Supabase persistence for the IMT Access integration.

Tables (see supabase/migration_imt_access.sql):
- imt_state        — singleton row: session token, last sync/run markers
- imt_resources    — catalog of portal e-resources
- imt_publications — daily publication items (EPW articles, FT/ET Prime stories)
- imt_reports      — generated analysis reports (markdown)

Every function degrades gracefully: when the migration has not been applied
yet, reads return empty defaults and writes are skipped with a warning, so the
rest of Mailbot is never broken by this feature.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any

from services.db import get_db

logger = logging.getLogger(__name__)

_tables_missing: set[str] = set()


def _handle_error(table: str, label: str, exc: Exception) -> None:
    """Log once when a table is missing; full trace for anything else."""
    err = str(exc).lower()
    if table in err or "does not exist" in err or "42p01" in err or "pgrst205" in err:
        if table not in _tables_missing:
            logger.warning(
                "%s table missing — run supabase/migration_imt_access.sql (%s)",
                table,
                exc,
            )
        _tables_missing.add(table)
        return
    logger.exception("%s failed: %s", label, exc)


def _table_ok(table: str) -> bool:
    return table not in _tables_missing


# --- imt_state (singleton) ---


def get_imt_state() -> dict[str, Any]:
    if not _table_ok("imt_state"):
        return {}
    try:
        db = get_db()
        result = db.table("imt_state").select("*").eq("id", 1).limit(1).execute()
        rows = result.data or []
        return rows[0] if rows else {}
    except Exception as exc:
        _handle_error("imt_state", "get_imt_state", exc)
        return {}


def update_imt_state(**fields: Any) -> None:
    if not fields or not _table_ok("imt_state"):
        return
    payload: dict[str, Any] = {"id": 1, "updated_at": datetime.now(timezone.utc).isoformat()}
    payload.update(fields)
    try:
        db = get_db()
        db.table("imt_state").upsert(payload, on_conflict="id").execute()
    except Exception as exc:
        _handle_error("imt_state", "update_imt_state", exc)


def get_session_token() -> str | None:
    state = get_imt_state()
    token = state.get("session_token")
    return str(token) if token else None


def save_session_token(token: str, user_display: str | None = None) -> None:
    update_imt_state(
        session_token=token,
        session_display_name=user_display,
        session_saved_at=datetime.now(timezone.utc).isoformat(),
    )


def daily_already_run(run_date: date) -> bool:
    state = get_imt_state()
    raw = state.get("last_daily_run_on")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)[:10]) == run_date
    except ValueError:
        return False


def mark_daily_run(run_date: date, *, error: str | None = None) -> None:
    update_imt_state(last_daily_run_on=run_date.isoformat(), last_error=error)


# --- imt_resources ---


def get_known_resources() -> dict[str, dict[str, Any]]:
    """All resources keyed by portal_id (active and inactive)."""
    if not _table_ok("imt_resources"):
        return {}
    try:
        db = get_db()
        result = (
            db.table("imt_resources")
            .select("portal_id, name, category, url, is_active, first_seen_at, last_seen_at")
            .execute()
        )
        return {str(r["portal_id"]): r for r in (result.data or []) if r.get("portal_id")}
    except Exception as exc:
        _handle_error("imt_resources", "get_known_resources", exc)
        return {}


def upsert_resources(rows: list[dict[str, Any]]) -> bool:
    if not rows or not _table_ok("imt_resources"):
        return False
    try:
        db = get_db()
        db.table("imt_resources").upsert(rows, on_conflict="portal_id").execute()
        return True
    except Exception as exc:
        _handle_error("imt_resources", "upsert_resources", exc)
        return False


def mark_resources_inactive(portal_ids: list[str]) -> None:
    if not portal_ids or not _table_ok("imt_resources"):
        return
    try:
        db = get_db()
        for pid in portal_ids:
            db.table("imt_resources").update({"is_active": False}).eq("portal_id", pid).execute()
    except Exception as exc:
        _handle_error("imt_resources", "mark_resources_inactive", exc)


def list_resources(*, active_only: bool = True) -> list[dict[str, Any]]:
    if not _table_ok("imt_resources"):
        return []
    try:
        db = get_db()
        query = (
            db.table("imt_resources")
            .select("portal_id, name, category, url, is_active, first_seen_at, last_seen_at")
            .order("category")
            .order("name")
        )
        if active_only:
            query = query.eq("is_active", True)
        result = query.execute()
        return result.data or []
    except Exception as exc:
        _handle_error("imt_resources", "list_resources", exc)
        return []


# --- imt_publications ---


def insert_publications(rows: list[dict[str, Any]]) -> int:
    """Insert publication items, ignoring duplicates. Returns upserted count."""
    if not rows or not _table_ok("imt_publications"):
        return 0
    inserted = 0
    try:
        db = get_db()
        for row in rows:
            try:
                result = (
                    db.table("imt_publications")
                    .upsert(row, on_conflict="kind,url,publication_date")
                    .execute()
                )
                inserted += len(result.data or [row])
            except Exception as exc:
                if "duplicate" in str(exc).lower() or "23505" in str(exc):
                    continue
                raise
        return inserted
    except Exception as exc:
        _handle_error("imt_publications", "insert_publications", exc)
        return inserted


def list_publications(*, limit: int = 60, kind: str | None = None) -> list[dict[str, Any]]:
    if not _table_ok("imt_publications"):
        return []
    try:
        db = get_db()
        query = (
            db.table("imt_publications")
            .select("id, kind, section, title, url, publication_date, summary, fetched_at")
            .order("publication_date", desc=True)
            .order("fetched_at", desc=True)
            .limit(limit)
        )
        if kind:
            query = query.eq("kind", kind)
        result = query.execute()
        return result.data or []
    except Exception as exc:
        _handle_error("imt_publications", "list_publications", exc)
        return []


# --- imt_reports ---


def save_report(kind: str, content: str) -> None:
    if not _table_ok("imt_reports"):
        return
    try:
        db = get_db()
        db.table("imt_reports").insert({"kind": kind, "content": content}).execute()
    except Exception as exc:
        _handle_error("imt_reports", "save_report", exc)


def get_latest_report(kind: str = "catalog_analysis") -> dict[str, Any] | None:
    if not _table_ok("imt_reports"):
        return None
    try:
        db = get_db()
        result = (
            db.table("imt_reports")
            .select("id, kind, content, created_at")
            .eq("kind", kind)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception as exc:
        _handle_error("imt_reports", "get_latest_report", exc)
        return None


# --- portal config (credential fallback for deploys without env access) ---

_PORTAL_CONFIG_KIND = "portal_config"


def save_portal_config(config: dict[str, str]) -> bool:
    """Persist portal credentials as an imt_reports row so a deployed instance
    without MAPMYACCESS_* env vars can still authenticate. Never exposed via
    the web API (report reads filter by kind)."""
    if not _table_ok("imt_reports"):
        return False
    try:
        db = get_db()
        db.table("imt_reports").insert(
            {"kind": _PORTAL_CONFIG_KIND, "content": json.dumps(config)}
        ).execute()
        return True
    except Exception as exc:
        _handle_error("imt_reports", "save_portal_config", exc)
        return False


def get_portal_config() -> dict[str, str]:
    row = get_latest_report(_PORTAL_CONFIG_KIND)
    if not row:
        return {}
    try:
        data = json.loads(row.get("content") or "{}")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}
