"""FastAPI dependencies for authenticated web API."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from services.users import get_owner_user, resolve_session_token


async def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    user = resolve_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


async def get_current_user_optional(authorization: str | None = Header(default=None)) -> dict | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return resolve_session_token(token)


def default_web_user() -> dict:
    """Legacy single-user web mode when Authorization is absent (tests / pre-migration)."""
    owner = get_owner_user()
    if owner:
        return owner
    from config import get_settings
    from services.user_ids import OWNER_SEED_USER_ID

    settings = get_settings()
    return {
        "id": OWNER_SEED_USER_ID,
        "role": "owner",
        "display_name": settings.student_name or "Owner",
        "telegram_chat_id": settings.telegram_owner_chat_id,
        "gmail_address": settings.gmail_user,
        "preferred_section": "A",
        "is_active": True,
    }


async def web_user(authorization: str | None = Header(default=None)) -> dict:
    """Authenticated user for web API; falls back to env owner before migration."""
    from services.db import multi_user_schema_ready

    if multi_user_schema_ready():
        return await get_current_user(authorization)
    return default_web_user()


def bind_mailbox_user(user: dict = Depends(web_user)):
    """Keep Gmail, deadlines, contacts, and the sign-off name on this user."""
    from gmail_api.auth import reset_gmail_user_context, set_gmail_user_context
    from services.mailbox_user import reset as reset_mailbox
    from services.mailbox_user import set_user

    mailbox_token = set_user(user)
    gmail_token = set_gmail_user_context(str(user["id"]))
    try:
        yield user
    finally:
        reset_gmail_user_context(gmail_token)
        reset_mailbox(mailbox_token)
