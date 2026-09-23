"""Multi-user accounts: owner seeding, invites, Telegram OTP login, web sessions."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from config import get_settings
from services.db import get_db
from services.user_ids import OWNER_SEED_USER_ID

logger = logging.getLogger(__name__)
SESSION_TTL_DAYS = 30
LOGIN_CODE_TTL_MINUTES = 10
LOGIN_CODE_MAX_ATTEMPTS = 5
LOGIN_CODE_RESEND_SECONDS = 60
INVITE_TTL_DAYS = 14

_users_table_missing = False
_last_code_sent: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_username(username: str | None) -> str:
    u = (username or "").strip().lstrip("@").lower()
    return u


def telegram_profile_display_name(
    first_name: str | None,
    last_name: str | None,
    username: str | None = None,
) -> str:
    """Real name from Telegram profile — never the @username (used for mail sign-offs)."""
    _ = username  # reserved for callers; usernames must not become display names
    parts: list[str] = []
    for raw in (first_name, last_name):
        bit = (raw or "").strip()
        if bit and not bit.startswith("["):
            parts.append(bit)
    return " ".join(parts)


def display_name_is_telegram_handle(name: str, user: dict[str, Any]) -> bool:
    """True when the stored name is just the Telegram @handle, not a person name."""
    n = (name or "").strip()
    if not n:
        return False
    handle = str(user.get("telegram_username") or "").lstrip("@").strip()
    if not handle:
        return False
    compact_name = n.lower().replace(" ", "").replace("_", "")
    compact_handle = handle.lower().replace("_", "")
    return compact_name == compact_handle


def _users_table_ok() -> bool:
    global _users_table_missing
    if _users_table_missing:
        return False
    try:
        get_db().table("users").select("id").limit(1).execute()
        return True
    except Exception as exc:
        err = str(exc).lower()
        if "users" in err and ("does not exist" in err or "42p01" in err):
            _users_table_missing = True
            return False
        return True


def row_to_user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "telegram_user_id": row.get("telegram_user_id"),
        "telegram_chat_id": row.get("telegram_chat_id"),
        "telegram_username": row.get("telegram_username"),
        "display_name": row.get("display_name") or "Student",
        "gmail_address": row.get("gmail_address"),
        "preferred_section": (row.get("preferred_section") or "A").strip().upper()[:1],
        "role": row.get("role") or "student",
        "is_active": bool(row.get("is_active", True)),
    }


def ensure_owner_user() -> dict[str, Any] | None:
    """Upsert seed owner from env; returns owner row or None if users table missing."""
    if not _users_table_ok():
        return None
    settings = get_settings()
    tg_uid = str(settings.telegram_owner_user_id or settings.telegram_owner_chat_id or "").strip()
    tg_chat = str(settings.telegram_owner_chat_id or tg_uid).strip()
    display = (settings.student_name or "Owner").strip()
    if display.startswith("["):
        display = "Owner"
    now = _now().isoformat()
    payload: dict[str, Any] = {
        "id": OWNER_SEED_USER_ID,
        "role": "owner",
        "display_name": display,
        "is_active": True,
        "updated_at": now,
    }
    if tg_uid:
        payload["telegram_user_id"] = tg_uid
    if tg_chat:
        payload["telegram_chat_id"] = tg_chat
    gmail = (settings.gmail_user or "").strip()
    if gmail:
        payload["gmail_address"] = gmail
    try:
        db = get_db()
        db.table("users").upsert(payload, on_conflict="id").execute()
        result = db.table("users").select("*").eq("id", OWNER_SEED_USER_ID).limit(1).execute()
        rows = result.data or []
        return row_to_user(rows[0]) if rows else row_to_user(payload)
    except Exception as exc:
        logger.exception("ensure_owner_user failed: %s", exc)
        return None


def get_owner_user() -> dict[str, Any] | None:
    if not _users_table_ok():
        return None
    try:
        result = (
            get_db()
            .table("users")
            .select("*")
            .eq("id", OWNER_SEED_USER_ID)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return row_to_user(rows[0]) if rows else ensure_owner_user()
    except Exception as exc:
        logger.warning("get_owner_user failed: %s", exc)
        return ensure_owner_user()


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    if not user_id or not _users_table_ok():
        return None
    try:
        result = (
            get_db()
            .table("users")
            .select("*")
            .eq("id", user_id)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return row_to_user(rows[0]) if rows else None
    except Exception as exc:
        logger.warning("get_user_by_id failed: %s", exc)
        return None


def get_user_by_telegram_id(telegram_user_id: str | int) -> dict[str, Any] | None:
    uid = str(telegram_user_id).strip()
    if not uid or not _users_table_ok():
        return None
    try:
        result = (
            get_db()
            .table("users")
            .select("*")
            .eq("telegram_user_id", uid)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        return row_to_user(rows[0]) if rows else None
    except Exception as exc:
        logger.warning("get_user_by_telegram_id failed: %s", exc)
        return None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    norm = _normalize_username(username)
    if not norm or not _users_table_ok():
        return None
    try:
        result = (
            get_db()
            .table("users")
            .select("*")
            .ilike("telegram_username", norm)
            .eq("is_active", True)
            .limit(5)
            .execute()
        )
        for row in result.data or []:
            if _normalize_username(row.get("telegram_username")) == norm:
                return row_to_user(row)
        return None
    except Exception as exc:
        logger.warning("get_user_by_username failed: %s", exc)
        return None


def list_users(*, active_only: bool = True) -> list[dict[str, Any]]:
    if not _users_table_ok():
        return []
    try:
        q = get_db().table("users").select("*").order("created_at")
        if active_only:
            q = q.eq("is_active", True)
        result = q.execute()
        return [row_to_user(r) for r in (result.data or [])]
    except Exception as exc:
        logger.warning("list_users failed: %s", exc)
        return []


def list_active_gmail_users() -> list[dict[str, Any]]:
    """Users who should be polled for Gmail (owner + students with tokens)."""
    owner = get_owner_user()
    users: list[dict[str, Any]] = []
    if owner:
        users.append(owner)
    if not _users_table_ok():
        return users
    try:
        result = (
            get_db()
            .table("users")
            .select("*")
            .eq("is_active", True)
            .neq("id", OWNER_SEED_USER_ID)
            .execute()
        )
        for row in result.data or []:
            users.append(row_to_user(row))
        return users
    except Exception as exc:
        logger.warning("list_active_gmail_users failed: %s", exc)
        return users


def user_has_gmail_token(user_id: str) -> bool:
    from services.db import get_stored_refresh_token

    token = get_stored_refresh_token("gmail", user_id=user_id)
    if token:
        return True
    if user_id == OWNER_SEED_USER_ID:
        settings = get_settings()
        return bool((settings.gmail_refresh_token or "").strip())
    return False


_gmail_name_checked: set[str] = set()


def stored_name_should_come_from_gmail(user: dict[str, Any] | None) -> bool:
    """True when the saved name is still a handle, roll number, or placeholder."""
    if not user or not str(user.get("gmail_address") or "").strip():
        return False
    name = str(user.get("display_name") or "").strip()
    if not name or name.startswith("[") or name.lower() in {"student", "owner"}:
        return True
    if display_name_is_telegram_handle(name, user):
        return True
    local = str(user.get("gmail_address")).split("@", 1)[0]
    compact = name.lower().replace(" ", "").replace(".", "")
    if local and compact == local.lower().replace(".", "").replace("_", ""):
        return True
    return " " not in name


def refresh_display_name_from_gmail(user: dict[str, Any] | None) -> dict[str, Any] | None:
    """Replace a Telegram handle with the Gmail account name, once per process."""
    if not user or not user.get("id"):
        return user
    uid = str(user["id"])
    if uid in _gmail_name_checked or not stored_name_should_come_from_gmail(user):
        return user
    try:
        from gmail_api.client import primary_mailbox_display_name

        gmail_name = primary_mailbox_display_name(uid)
    except Exception as exc:
        logger.warning("Could not read Gmail display name for %s: %s", uid, exc)
        return user
    _gmail_name_checked.add(uid)
    if not gmail_name or gmail_name == user.get("display_name"):
        return user
    updated = update_user_fields(uid, {"display_name": gmail_name})
    return updated or {**user, "display_name": gmail_name}


def update_user_fields(user_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    if not fields or not _users_table_ok():
        return None
    allowed = {
        "telegram_user_id",
        "telegram_chat_id",
        "telegram_username",
        "display_name",
        "gmail_address",
        "preferred_section",
        "is_active",
    }
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return get_user_by_id(user_id)
    payload["updated_at"] = _now().isoformat()
    if "preferred_section" in payload and payload["preferred_section"]:
        payload["preferred_section"] = str(payload["preferred_section"]).strip().upper()[:1]
    try:
        result = get_db().table("users").update(payload).eq("id", user_id).execute()
        rows = result.data or []
        if rows:
            return row_to_user(rows[0])
        return get_user_by_id(user_id)
    except Exception as exc:
        logger.exception("update_user_fields failed: %s", exc)
        return None


def deactivate_user(user_id: str) -> bool:
    if user_id == OWNER_SEED_USER_ID:
        return False
    row = update_user_fields(user_id, {"is_active": False})
    return row is not None


def create_invite(telegram_username: str, *, created_by: str | None = None) -> dict[str, Any] | None:
    norm = _normalize_username(telegram_username)
    if not norm or not _users_table_ok():
        return None
    code = secrets.token_urlsafe(12)
    expires = _now() + timedelta(days=INVITE_TTL_DAYS)
    try:
        db = get_db()
        db.table("user_invites").insert(
            {
                "telegram_username": norm,
                "invite_code": code,
                "expires_at": expires.isoformat(),
            }
        ).execute()
        return {"username": norm, "invite_code": code, "expires_at": expires.isoformat()}
    except Exception as exc:
        logger.exception("create_invite failed: %s", exc)
        return None


def get_pending_invite_for_username(username: str) -> dict[str, Any] | None:
    norm = _normalize_username(username)
    if not norm or not _users_table_ok():
        return None
    try:
        now = _now().isoformat()
        result = (
            get_db()
            .table("user_invites")
            .select("*")
            .ilike("telegram_username", norm)
            .is_("claimed_by", "null")
            .gt("expires_at", now)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        for row in result.data or []:
            if _normalize_username(row.get("telegram_username")) == norm:
                return row
        return None
    except Exception as exc:
        logger.warning("get_pending_invite_for_username failed: %s", exc)
        return None


def claim_invite(
    invite_code: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    telegram_last_name: str | None = None,
) -> dict[str, Any] | None:
    code = (invite_code or "").strip()
    if not code or not _users_table_ok():
        return None
    try:
        db = get_db()
        result = (
            db.table("user_invites")
            .select("*")
            .eq("invite_code", code)
            .is_("claimed_by", "null")
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            already = (
                db.table("user_invites")
                .select("*")
                .eq("invite_code", code)
                .limit(1)
                .execute()
            )
            claimed_rows = already.data or []
            if claimed_rows:
                existing = get_user_by_telegram_id(telegram_user_id)
                if existing and str(existing.get("id")) == str(claimed_rows[0].get("claimed_by")):
                    return existing
            return None
        invite = rows[0]
        exp = invite.get("expires_at")
        if exp and str(exp) < _now().isoformat():
            return None
        norm_inv = _normalize_username(invite.get("telegram_username"))
        norm_user = _normalize_username(telegram_username)
        if norm_user and norm_inv and norm_user != norm_inv:
            return {"_claim_error": "username_mismatch", "expected": norm_inv}
        display = telegram_profile_display_name(
            telegram_first_name, telegram_last_name, telegram_username
        ) or "Student"
        user_row = (
            db.table("users")
            .insert(
                {
                    "telegram_user_id": str(telegram_user_id),
                    "telegram_chat_id": str(telegram_chat_id),
                    "telegram_username": norm_user or norm_inv,
                    "display_name": display,
                    "role": "student",
                    "is_active": True,
                }
            )
            .execute()
        )
        created = (user_row.data or [None])[0]
        if not created:
            existing = get_user_by_telegram_id(telegram_user_id)
            if not existing:
                return None
            user_id = existing["id"]
        else:
            user_id = str(created["id"])
        db.table("user_invites").update({"claimed_by": user_id}).eq("id", invite["id"]).execute()
        return get_user_by_id(user_id)
    except Exception as exc:
        logger.exception("claim_invite failed: %s", exc)
        return None


def link_telegram_to_user(
    user_id: str,
    *,
    telegram_user_id: str,
    telegram_chat_id: str,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    telegram_last_name: str | None = None,
) -> dict[str, Any] | None:
    fields: dict[str, Any] = {
        "telegram_user_id": str(telegram_user_id),
        "telegram_chat_id": str(telegram_chat_id),
        "telegram_username": _normalize_username(telegram_username) or None,
    }
    existing = get_user_by_id(user_id)
    profile_name = telegram_profile_display_name(
        telegram_first_name, telegram_last_name, telegram_username
    )
    if profile_name and existing and display_name_is_telegram_handle(
        str(existing.get("display_name") or ""), existing
    ):
        fields["display_name"] = profile_name
    return update_user_fields(user_id, fields)


def _generate_login_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def request_login_code(username: str) -> dict[str, Any]:
    """Returns {ok, message, needs_bot_start?, invite_code?}."""
    norm = _normalize_username(username)
    if not norm:
        return {"ok": False, "message": "Enter your Telegram username."}
    if not _users_table_ok():
        return {"ok": False, "message": "Login is not available yet (database migration pending)."}

    import time

    now_mono = time.monotonic()
    last = _last_code_sent.get(norm, 0)
    if now_mono - last < LOGIN_CODE_RESEND_SECONDS:
        wait = int(LOGIN_CODE_RESEND_SECONDS - (now_mono - last))
        return {"ok": False, "message": f"Wait {wait}s before requesting another code."}

    user = get_user_by_username(norm)
    if not user:
        invite = get_pending_invite_for_username(norm)
        if not invite:
            return {"ok": False, "message": "You are not invited yet. Ask the bot owner for an invite."}
        return {
            "ok": False,
            "needs_bot_start": True,
            "invite_code": invite.get("invite_code"),
            "message": "Open the bot in Telegram first (use the invite link), then request a code again.",
        }

    if not user.get("telegram_chat_id") and not user.get("telegram_user_id"):
        invite = get_pending_invite_for_username(norm)
        if invite:
            return {
                "ok": False,
                "needs_bot_start": True,
                "invite_code": invite.get("invite_code"),
                "message": "Start the bot in Telegram first, then request a code again.",
            }
        return {"ok": False, "message": "Send /start to the bot in Telegram, then try again."}

    code = _generate_login_code()
    code_hash = _hash_token(code)
    expires = _now() + timedelta(minutes=LOGIN_CODE_TTL_MINUTES)
    try:
        db = get_db()
        db.table("login_codes").insert(
            {
                "user_id": user["id"],
                "code_hash": code_hash,
                "expires_at": expires.isoformat(),
            }
        ).execute()
    except Exception as exc:
        logger.exception("request_login_code insert failed: %s", exc)
        return {"ok": False, "message": "Could not create login code. Try again."}

    _last_code_sent[norm] = now_mono
    return {
        "ok": True,
        "message": "Check Telegram for your login code.",
        "user_id": user["id"],
        "_code_for_bot": code,
    }


def discard_latest_login_code(user_id: str, username: str) -> None:
    """Drop an unused code so a failed Telegram send does not lock the user out."""
    norm = _normalize_username(username)
    if norm:
        _last_code_sent.pop(norm, None)
    try:
        db = get_db()
        (
            db.table("login_codes")
            .delete()
            .eq("user_id", user_id)
            .is_("consumed_at", "null")
            .execute()
        )
    except Exception as exc:
        logger.warning("discard_latest_login_code failed: %s", exc)


def verify_login_code(username: str, code: str) -> dict[str, Any]:
    norm = _normalize_username(username)
    code_clean = re.sub(r"\D", "", code or "")
    if not norm or len(code_clean) != 6:
        return {"ok": False, "message": "Invalid username or code."}
    user = get_user_by_username(norm)
    if not user:
        return {"ok": False, "message": "User not found."}
    try:
        db = get_db()
        result = (
            db.table("login_codes")
            .select("*")
            .eq("user_id", user["id"])
            .is_("consumed_at", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return {"ok": False, "message": "No active code. Request a new one."}
        row = rows[0]
        if str(row.get("expires_at", "")) < _now().isoformat():
            return {"ok": False, "message": "Code expired. Request a new one."}
        attempts = int(row.get("attempts") or 0)
        if attempts >= LOGIN_CODE_MAX_ATTEMPTS:
            return {"ok": False, "message": "Too many attempts. Request a new code."}
        expected = row.get("code_hash") or ""
        if not hmac.compare_digest(expected, _hash_token(code_clean)):
            db.table("login_codes").update({"attempts": attempts + 1}).eq("id", row["id"]).execute()
            return {"ok": False, "message": "Wrong code."}
        db.table("login_codes").update({"consumed_at": _now().isoformat()}).eq("id", row["id"]).execute()
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        expires = _now() + timedelta(days=SESSION_TTL_DAYS)
        db.table("web_sessions").upsert(
            {
                "token_hash": token_hash,
                "user_id": user["id"],
                "expires_at": expires.isoformat(),
                "last_seen_at": _now().isoformat(),
            },
            on_conflict="token_hash",
        ).execute()
        public_user = {k: v for k, v in user.items() if not k.startswith("_")}
        return {"ok": True, "token": token, "user": public_user}
    except Exception as exc:
        logger.exception("verify_login_code failed: %s", exc)
        return {"ok": False, "message": "Verification failed."}


def resolve_session_token(raw_token: str | None) -> dict[str, Any] | None:
    if not raw_token or not _users_table_ok():
        return None
    token_hash = _hash_token(raw_token.strip())
    try:
        db = get_db()
        result = (
            db.table("web_sessions")
            .select("user_id, expires_at")
            .eq("token_hash", token_hash)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return None
        row = rows[0]
        if str(row.get("expires_at", "")) < _now().isoformat():
            db.table("web_sessions").delete().eq("token_hash", token_hash).execute()
            return None
        db.table("web_sessions").update({"last_seen_at": _now().isoformat()}).eq(
            "token_hash", token_hash
        ).execute()
        return get_user_by_id(str(row["user_id"]))
    except Exception as exc:
        logger.warning("resolve_session_token failed: %s", exc)
        return None


def revoke_session_token(raw_token: str | None) -> None:
    if not raw_token or not _users_table_ok():
        return
    try:
        get_db().table("web_sessions").delete().eq("token_hash", _hash_token(raw_token.strip())).execute()
    except Exception as exc:
        logger.warning("revoke_session_token failed: %s", exc)


def is_owner_user(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("role") == "owner")


def list_watcher_targets() -> list[dict[str, Any]]:
    """Users whose Gmail inboxes should be polled."""
    ensure_owner_user()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list_active_gmail_users():
        uid = row["id"]
        if uid in seen:
            continue
        if not user_has_gmail_token(uid):
            continue
        seen.add(uid)
        out.append(row)
    owner = get_owner_user()
    if owner and owner["id"] not in seen and user_has_gmail_token(owner["id"]):
        out.insert(0, owner)
    elif not out and owner:
        out.append(owner)
    return out
