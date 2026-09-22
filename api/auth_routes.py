"""Telegram OTP login and session management for the web app."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from api.deps import get_current_user
from config import get_settings
from services.users import (
    discard_latest_login_code,
    get_user_by_id,
    request_login_code,
    revoke_session_token,
    verify_login_code,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RequestCodeBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class VerifyCodeBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=8)


async def _telegram_send(chat_id: str, text: str) -> bool:
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        return resp.status_code < 400
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)
        return False


@router.post("/request-code")
async def auth_request_code(body: RequestCodeBody):
    result = request_login_code(body.username)
    if not result.get("ok"):
        return {
            "ok": False,
            "message": result.get("message"),
            "needs_bot_start": result.get("needs_bot_start", False),
            "invite_code": result.get("invite_code"),
        }
    code = result.pop("_code_for_bot", None)
    user_id = result.get("user_id")
    if code and user_id:
        user_row = get_user_by_id(str(user_id))
        chat_id = (user_row or {}).get("telegram_chat_id")
        if not chat_id:
            return {
                "ok": False,
                "message": "Start the bot in Telegram first (/start), then request a code again.",
            }
        sent = await _telegram_send(
            str(chat_id),
            f"Your Mailbot web login code: <b>{code}</b>\n\n"
            "It expires in 10 minutes. Do not share this code.",
        )
        if not sent:
            discard_latest_login_code(str(user_id), body.username)
            return {"ok": False, "message": "Could not send code via Telegram. Try /login in the bot."}
    return {"ok": True, "message": result.get("message", "Check Telegram for your code.")}


@router.post("/verify")
async def auth_verify(body: VerifyCodeBody):
    result = verify_login_code(body.username, body.code)
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=result.get("message") or "Invalid code")
    return {"ok": True, "token": result["token"], "user": result["user"]}


@router.get("/me")
async def auth_me(user: dict = Depends(get_current_user)):
    return {"user": user}


@router.post("/logout")
async def auth_logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        revoke_session_token(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@router.get("/gmail/connect-url")
async def gmail_connect_url(user: dict = Depends(get_current_user)):
    from services.oauth_state import create_oauth_state

    settings = get_settings()
    base = (settings.render_url or settings.gmail_oauth_redirect_uri or "").rstrip("/")
    if not base or "your-app" in base:
        raise HTTPException(status_code=503, detail="Set RENDER_URL or GMAIL_OAUTH_REDIRECT_URI")
    state = create_oauth_state(user["id"])
    return {"url": f"{base}/oauth/start?s={state}"}
