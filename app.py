import hashlib
import asyncio
import json
import logging
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api.auth_routes import router as auth_api_router
from api.web_routes import router as web_api_router
from google.auth.exceptions import RefreshError
from telegram import Update

from bot.telegram_app import build_telegram_application, register_bot_commands
from config import get_settings
from services.alert_delivery import send_owner_update
from services.db import check_db_health
from services.digest import run_evening_digest, run_morning_digest
from services.formatting import escape_html as _escape_html
from services.oauth_state import create_oauth_state, verify_oauth_state
from bot.whatsapp_router import process_whatsapp_message
from services.messaging.outbox import take_outbound
from services.messaging.pending_digest import stash_and_nudge
from services.messaging.webhook import extract_inbound_messages, extract_status_events
from services.messaging.whatsapp import errors_indicate_outside_window, get_whatsapp_client
from services.telegram_dedupe import should_process_update, should_process_webhook_event
from services.timeutil import digest_window_open, now_ist
from services.monitoring import ping_heartbeat
from services.reminders import run_scheduled_reminders
from services.watcher import (
    WATCHER_INTERVAL_SECONDS,
    notify_gmail_auth_failure,
    run_due_reminders,
    run_gmail_backfill,
    run_gmail_reconnect_backfill,
    run_gmail_watcher,
    watcher_lock,
)
from services.websocket_manager import broadcast_fire_and_forget, connect, disconnect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Avoid logging full Telegram API URLs (token in path) from PTB/httpx at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ptb_app = build_telegram_application()

# Render free tier sleeps after ~15 min of no inbound HTTP traffic.
# Ping well inside that window and retry quickly so a single slow/failed
# request can never open a >15 min gap.
KEEP_ALIVE_INTERVAL_SECONDS = 10 * 60
KEEP_ALIVE_RETRY_SECONDS = 30
KEEP_ALIVE_MAX_RETRIES = 3
DIGEST_SCHEDULER_INTERVAL_SECONDS = 60
MORNING_DIGEST_HOUR = 7
MORNING_DIGEST_END_HOUR = 16
EVENING_DIGEST_HOUR = 20
EVENING_DIGEST_END_HOUR = 24
# IMT library daily briefing: fires after the morning digest window opens.
IMT_DAILY_START_HOUR = 7
IMT_DAILY_START_MINUTE = 15
IMT_DAILY_END_HOUR = 16
PING_BODY = b"ok"

digest_scheduler_lock = asyncio.Lock()
imt_daily_lock = asyncio.Lock()


class InstantPingMiddleware:
    """Answer /ping before FastAPI lifespan finishes starting Telegram/Gmail."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") in ("/ping", "/ping/"):
            body = b"" if scope["method"] == "HEAD" else PING_BODY
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-type", b"text/plain; charset=utf-8"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


async def _keep_alive_loop() -> None:
    """Ping our own public URL so Render free tier stays awake.

    NOTE: this only keeps an already-running instance awake. It cannot wake a
    sleeping instance (the process is gone). An external pinger is still needed
    as the primary safety net.
    """
    settings = get_settings()
    base = (settings.render_url or "").rstrip("/")
    if not base or "your-app" in base:
        logger.info("Keep-alive disabled (RENDER_URL not set)")
        return

    url = f"{base}/ping"
    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            ok = False
            for attempt in range(1, KEEP_ALIVE_MAX_RETRIES + 1):
                try:
                    resp = await client.get(url)
                    logger.info("Self-ping ok %s -> %s", url, resp.status_code)
                    ok = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Self-ping attempt %s/%s failed: %s",
                        attempt,
                        KEEP_ALIVE_MAX_RETRIES,
                        exc,
                    )
                    if attempt < KEEP_ALIVE_MAX_RETRIES:
                        await asyncio.sleep(KEEP_ALIVE_RETRY_SECONDS)
            await asyncio.sleep(
                KEEP_ALIVE_INTERVAL_SECONDS if ok else KEEP_ALIVE_RETRY_SECONDS
            )


async def _background_watcher_loop() -> None:
    """Poll Gmail every ~60s while the process is awake."""
    reminder_counter = 0
    while True:
        watcher_ok = False
        try:
            async with watcher_lock:
                result = await run_gmail_watcher(ptb_app.bot)
            watcher_ok = True
            logger.info("Background watcher result: %s", result)
            broadcast_fire_and_forget("sync_status", {"watcher": result})
            await ping_heartbeat(ok=True)
            from services.db import update_watch_state

            await asyncio.to_thread(
                update_watch_state,
                last_heartbeat_at=datetime.now(timezone.utc),
            )
            reminder_counter += 1
            if reminder_counter >= 3:
                reminder_counter = 0
                try:
                    remind_result = await run_due_reminders(ptb_app.bot)
                    if remind_result.get("reminders_sent"):
                        logger.info("Due reminders: %s", remind_result)
                except Exception as exc:
                    logger.exception("Due reminders failed: %s", exc)
                try:
                    sched_result = await run_scheduled_reminders(ptb_app.bot)
                    if sched_result.get("reminders_sent"):
                        logger.info("Scheduled reminders: %s", sched_result)
                except Exception as exc:
                    logger.exception("Scheduled reminders failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except RefreshError:
            await ping_heartbeat(ok=False)
            try:
                await notify_gmail_auth_failure(ptb_app.bot)
            except Exception as exc:
                logger.exception("Gmail auth alert failed: %s", exc)
        except Exception as exc:
            logger.exception("Background watcher failed: %s", exc)
            if watcher_ok:
                await ping_heartbeat(ok=False)
        await asyncio.sleep(WATCHER_INTERVAL_SECONDS)


async def _digest_scheduler_loop() -> None:
    """Fire morning (07:00-16:00 IST) and evening (20:00-24:00 IST) digests while awake."""
    while True:
        try:
            now = now_ist()
            async with digest_scheduler_lock:
                if MORNING_DIGEST_HOUR <= now.hour < MORNING_DIGEST_END_HOUR:
                    result = await run_morning_digest(ptb_app.bot)
                    if result.get("sent"):
                        logger.info("In-app morning digest: %s", result)
                    elif result.get("skipped"):
                        pass  # already sent today — expected on subsequent ticks
                if EVENING_DIGEST_HOUR <= now.hour < EVENING_DIGEST_END_HOUR:
                    result = await run_evening_digest(ptb_app.bot)
                    if result.get("sent"):
                        logger.info("In-app evening digest: %s", result)
                    elif result.get("skipped"):
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Digest scheduler failed: %s", exc)
        await asyncio.sleep(DIGEST_SCHEDULER_INTERVAL_SECONDS)


async def _imt_daily_scheduler_loop() -> None:
    """Fire the IMT library daily briefing (07:15-16:00 IST) while awake.

    Idempotent per IST day via imt_state — the GitHub Actions cron backup can
    hit /tasks/imt-daily on the same day without double-sending.
    """
    from imt_access.jobs import run_daily_job

    while True:
        try:
            now = now_ist()
            window_open = (now.hour, now.minute) >= (IMT_DAILY_START_HOUR, IMT_DAILY_START_MINUTE)
            window_open = window_open and now.hour < IMT_DAILY_END_HOUR
            if window_open:
                async with imt_daily_lock:
                    result = await run_daily_job(ptb_app.bot)
                if result.get("ok") and not result.get("skipped"):
                    logger.info("In-app IMT daily briefing: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("IMT daily scheduler failed: %s", exc)
        await asyncio.sleep(DIGEST_SCHEDULER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.users import ensure_owner_user

    ensure_owner_user()
    await ptb_app.initialize()
    await ptb_app.start()
    await register_bot_commands(ptb_app)
    logger.info("Telegram application started")
    watcher_task = asyncio.create_task(_background_watcher_loop())
    keep_alive_task = asyncio.create_task(_keep_alive_loop())
    digest_task = asyncio.create_task(_digest_scheduler_loop())
    imt_daily_task = asyncio.create_task(_imt_daily_scheduler_loop())
    yield
    for task in (watcher_task, keep_alive_task, digest_task, imt_daily_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("Telegram application stopped")


fastapi_app = FastAPI(title="Mailbot", lifespan=lifespan)
fastapi_app.include_router(auth_api_router)
fastapi_app.include_router(web_api_router)

WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"


@fastapi_app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = Query(default=None)):
    from services.users import resolve_session_token

    user = resolve_session_token(token) if token else None
    user_id = user["id"] if user else None
    await connect(ws, user_id=user_id)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await disconnect(ws)
    except Exception:
        await disconnect(ws)


@fastapi_app.get("/healthz")
async def healthz():
    from ai.client import get_llm_status

    db = check_db_health()
    status = "ok" if db.get("ok") else "degraded"
    return {"status": status, "llm": get_llm_status(), "db": db}


@fastapi_app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    """Public privacy policy (required by Meta to switch the app to Live mode)."""
    settings = get_settings()
    name = _escape_html(settings.student_name or "Mailbot operator")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Mailbot Privacy Policy</title></head>
<body>
<h1>Privacy Policy</h1>
<p><strong>Mailbot</strong> is a personal assistant operated by {name}. It connects
Gmail and messaging apps (Telegram, WhatsApp) to send task reminders and email alerts.</p>
<h2>Data we process</h2>
<ul>
<li>Email metadata and content from the linked Gmail account (classification and alerts only).</li>
<li>Messages you send to the bot on Telegram or WhatsApp (commands and chat).</li>
<li>Task and reminder data stored in our database (Supabase).</li>
</ul>
<h2>How we use it</h2>
<p>Data is used only to run the assistant for the account owner. We do not sell or share
personal data with third parties except service providers (hosting, email, messaging APIs)
needed to operate the bot.</p>
<h2>Retention &amp; deletion</h2>
<p>Data is kept while the service is in use. To request deletion, contact the operator
via the same Telegram or email account linked to the bot.</p>
<p><em>Last updated: September 2026</em></p>
</body></html>"""


async def _process_whatsapp_payload(payload: dict) -> None:
    client = get_whatsapp_client()
    for status in extract_status_events(payload):
        state = str(status.get("status") or "").lower()
        if state != "failed":
            continue
        errors = status.get("errors") or status
        logger.warning(
            "WhatsApp delivery failed for message %s to %s: %s",
            status.get("id"),
            status.get("recipient_id"),
            errors,
        )
        # Meta sometimes accepts a free-form message and only fails it here,
        # asynchronously (e.g. 131047 outside the 24h window). Recover the
        # tracked payload: stash it and nudge the owner with the approved
        # template so the alert is not silently dropped.
        if not errors_indicate_outside_window(errors):
            continue
        phone = str(status.get("recipient_id") or "")
        item = await take_outbound(phone, str(status.get("id") or ""))
        if not item:
            continue
        logger.info(
            "Stashing undelivered WhatsApp %s for %s after async delivery failure",
            item.get("kind") or "alert",
            phone,
        )
        await stash_and_nudge(
            phone,
            kind=str(item.get("kind") or "alert"),
            text=str(item.get("text") or ""),
        )
    messages = extract_inbound_messages(payload)
    if not messages:
        logger.debug("WhatsApp webhook POST with no inbound messages")
        return
    for phone, msg in messages:
        msg_id = str(msg.get("id") or "")
        if not await should_process_webhook_event(msg_id):
            logger.info("Skipping duplicate WhatsApp message id=%s", msg_id)
            continue
        if msg_id:
            # Don't wait for Meta's read-receipt — start the handler immediately.
            asyncio.create_task(client.mark_read(msg_id, typing=True))
        try:
            await process_whatsapp_message(phone, msg)
        except Exception as exc:
            logger.exception("WhatsApp message handling failed: %s", exc)


@fastapi_app.get("/whatsapp/webhook")
async def whatsapp_webhook_verify(request: Request):
    """Meta sends hub.mode, hub.verify_token, hub.challenge as query params."""
    params = request.query_params
    hub_mode = params.get("hub.mode")
    hub_verify_token = (params.get("hub.verify_token") or "").strip()
    hub_challenge = params.get("hub.challenge") or ""

    settings = get_settings()
    expected = (settings.whatsapp_verify_token or "").strip()

    if hub_mode != "subscribe":
        logger.warning("WhatsApp webhook verify: unexpected hub.mode=%s", hub_mode)
        raise HTTPException(status_code=403, detail="Invalid verify token")

    if not expected:
        logger.warning("WhatsApp webhook verify: WHATSAPP_VERIFY_TOKEN is not set on server")
        raise HTTPException(status_code=403, detail="Invalid verify token")

    if not hmac.compare_digest(hub_verify_token, expected):
        logger.warning(
            "WhatsApp webhook verify: token mismatch (len sent=%s, len expected=%s)",
            len(hub_verify_token),
            len(expected),
        )
        raise HTTPException(status_code=403, detail="Invalid verify token")

    return PlainTextResponse(content=hub_challenge)


@fastapi_app.post("/whatsapp/webhook")
async def whatsapp_webhook(request: Request) -> Response:
    settings = get_settings()
    body = await request.body()
    logger.info("WhatsApp webhook POST (%s bytes)", len(body))
    secret = (settings.whatsapp_app_secret or "").strip()
    if secret:
        signature = request.headers.get("X-Hub-Signature-256") or ""
        expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("WhatsApp webhook POST rejected: invalid X-Hub-Signature-256")
            raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    asyncio.create_task(_process_whatsapp_payload(payload))
    return Response(status_code=200)


@fastapi_app.post(f"/telegram/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    settings = get_settings()
    if not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    if not await should_process_update(update.update_id):
        logger.info("Skipping duplicate Telegram update_id=%s", update.update_id)
        return Response(status_code=200)
    await ptb_app.process_update(update)
    return Response(status_code=200)


@fastapi_app.post("/tasks/watch-gmail")
async def watch_gmail(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    try:
        async with watcher_lock:
            result = await run_gmail_watcher(ptb_app.bot)
    except RefreshError as exc:
        logger.exception("Gmail auth failed during watch-gmail: %s", exc)
        await notify_gmail_auth_failure(ptb_app.bot)
        await ping_heartbeat(ok=False)
        raise HTTPException(
            status_code=503,
            detail="Gmail refresh token expired or revoked; use /oauth/start to reconnect",
        ) from exc
    await ping_heartbeat(ok=True)
    from services.db import update_watch_state

    await asyncio.to_thread(update_watch_state, last_heartbeat_at=datetime.now(timezone.utc))
    remind_result = await run_due_reminders(ptb_app.bot)
    result["reminders"] = remind_result
    sched_result = await run_scheduled_reminders(ptb_app.bot)
    result["scheduled_reminders"] = sched_result
    logger.info("Gmail watcher result: %s", result)
    return result


@fastapi_app.post("/tasks/backfill")
async def backfill_gmail(
    days: int = 14,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    if days < 1 or days > 30:
        raise HTTPException(status_code=400, detail="days must be between 1 and 30")

    async with watcher_lock:
        result = await run_gmail_backfill(ptb_app.bot, days=days)
    logger.info("Gmail backfill result: %s", result)
    return result


@fastapi_app.post("/tasks/digest-morning")
async def digest_morning(
    dry_run: int = 0,
    force: int = 0,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    if not dry_run and not force and not digest_window_open("morning"):
        logger.info("Morning digest skipped: outside 07:00–16:00 IST")
        return {"ok": True, "skipped": "outside_window"}

    result = await run_morning_digest(
        ptb_app.bot, dry_run=bool(dry_run), force=bool(force)
    )
    logger.info("Morning digest result: %s", result)
    return result


@fastapi_app.post("/tasks/digest-evening")
async def digest_evening(
    dry_run: int = 0,
    force: int = 0,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    if not dry_run and not force and not digest_window_open("evening"):
        logger.info("Evening digest skipped: outside 20:00–24:00 IST")
        return {"ok": True, "skipped": "outside_window"}

    result = await run_evening_digest(
        ptb_app.bot, dry_run=bool(dry_run), force=bool(force)
    )
    logger.info("Evening digest result: %s", result)
    return result


@fastapi_app.post("/tasks/imt-daily")
async def imt_daily_task(
    dry_run: int = 0,
    force: int = 0,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    """IMT library daily briefing (EPW + FT + ET Prime) — Telegram/WhatsApp/web."""
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    from imt_access.jobs import run_daily_job

    if not dry_run and not force and not digest_window_open("library"):
        logger.info("Library digest skipped: outside 07:15–16:00 IST")
        return {"ok": True, "skipped": "outside_window"}

    async with imt_daily_lock:
        result = await run_daily_job(
            ptb_app.bot, dry_run=bool(dry_run), force=bool(force)
        )
    logger.info("IMT daily task result: %s", result)
    return result


@fastapi_app.post("/tasks/imt-sync-catalog")
async def imt_sync_catalog_task(x_task_secret: str = Header(..., alias="x-task-secret")):
    """Refresh the e-resources catalog from the MapMyAccess portal."""
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    from imt_access.jobs import run_catalog_sync

    result = await run_catalog_sync(bot=ptb_app.bot)
    logger.info("IMT catalog sync result: %s", result)
    return result


@fastapi_app.post("/tasks/run-reminders")
async def run_reminders_task(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    due_result = await run_due_reminders(ptb_app.bot)
    sched_result = await run_scheduled_reminders(ptb_app.bot)
    return {"due_reminders": due_result, "scheduled_reminders": sched_result}


@fastapi_app.post("/tasks/notify-owner")
async def notify_owner_task(request: Request, x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    payload = payload or {}
    custom = str(payload.get("message") or "").strip()
    url = str(payload.get("url") or payload.get("text") or "").strip()
    if custom:
        html = custom
    elif url:
        html = (
            "<b>Mailbot website is live</b>\n\n"
            f"{_escape_html(url)}\n\n"
            "Open it on your phone — Home, Tasks, Chat, and Schedule."
        )
    else:
        raise HTTPException(status_code=400, detail="message or url is required")
    sent = await send_owner_update(ptb_app.bot, settings.telegram_owner_chat_id, html)
    return {"ok": bool(sent)}


@fastapi_app.get("/oauth/start")
async def oauth_start(token: str | None = Query(default=None), s: str | None = Query(default=None)):
    """Redirect owner or student to Google consent screen for Gmail connect."""
    settings = get_settings()
    user_id = None
    if s:
        ok, user_id = verify_oauth_state(s)
        if not ok:
            raise HTTPException(status_code=403, detail="Invalid OAuth state")
    else:
        expected = hashlib.sha256(f"oauth:{settings.task_secret}".encode()).hexdigest()[:32]
        if not token or not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail="Invalid OAuth start token")

    redirect_uri = settings.gmail_oauth_redirect_uri
    if not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="Set GMAIL_OAUTH_REDIRECT_URI to enable one-tap reconnect",
        )
    state = create_oauth_state(user_id)
    params = urlencode(
        {
            "client_id": settings.gmail_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(
                [
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.send",
                    "https://www.googleapis.com/auth/gmail.modify",
                ]
            ),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )

    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@fastapi_app.get("/oauth/callback")
async def oauth_callback(
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    """Exchange OAuth code for refresh token and run reconnect backfill."""
    if error or not code:
        safe_error = _escape_html(error or "No authorization code")
        return HTMLResponse(
            f"<h1>Gmail reconnect failed</h1><p>{safe_error}</p>",
            status_code=400,
        )

    ok_state, oauth_user_id = verify_oauth_state(state)
    if not ok_state:
        return HTMLResponse(
            "<h1>Gmail reconnect failed</h1><p>Invalid or expired OAuth state.</p>",
            status_code=403,
        )

    settings = get_settings()
    redirect_uri = settings.gmail_oauth_redirect_uri
    if not redirect_uri:
        raise HTTPException(status_code=503, detail="GMAIL_OAUTH_REDIRECT_URI not configured")

    token_url = "https://oauth2.googleapis.com/token"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            token_url,
            data={
                "code": code,
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code >= 400:
        logger.error("OAuth token exchange failed: %s", resp.text)
        return HTMLResponse("<h1>Token exchange failed</h1>", status_code=502)

    data = resp.json()
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return HTMLResponse(
            "<h1>No refresh token returned</h1><p>Try again with prompt=consent.</p>",
            status_code=502,
        )

    from gmail_api.auth import store_refresh_token
    from gmail_api.client import get_gmail_service

    store_refresh_token(refresh_token, user_id=oauth_user_id)
    from gmail_api.auth import reset_gmail_user_context, set_gmail_user_context
    from services.mailbox_user import reset as reset_mb
    from services.mailbox_user import set_user as set_mb_user
    from services.users import get_user_by_id, update_user_fields

    urow = get_user_by_id(oauth_user_id) if oauth_user_id else None
    if urow:
        mb = set_mb_user(urow)
        gt = set_gmail_user_context(oauth_user_id)
        try:
            profile = get_gmail_service(oauth_user_id).users().getProfile(userId="me").execute()
            email = profile.get("emailAddress")
            if email:
                update_user_fields(oauth_user_id, {"gmail_address": email})
        except Exception as exc:
            logger.warning("Could not fetch Gmail profile after connect: %s", exc)
        finally:
            reset_gmail_user_context(gt)
            reset_mb(mb)
    backfill = await run_gmail_reconnect_backfill(ptb_app.bot)
    logger.info("Gmail reconnected; backfill: %s", backfill)
    return HTMLResponse(
        "<h1>Gmail reconnected</h1><p>You can close this tab and return to Telegram.</p>"
    )


assets_dir = WEB_DIST / "assets"
if assets_dir.is_dir():
    fastapi_app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")


def _web_index_response() -> FileResponse:
    index = WEB_DIST / "index.html"
    if not index.is_file():
        logger.error("Web frontend missing at %s; deployment build did not include web/dist", index)
        raise HTTPException(status_code=503, detail="Web frontend is unavailable")
    return FileResponse(
        index,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@fastapi_app.get("/")
async def web_app_root():
    return _web_index_response()


@fastapi_app.get("/{spa_path:path}")
async def web_app_spa(spa_path: str):
    blocked_prefixes = (
        "api/",
        "telegram/",
        "whatsapp/",
        "tasks/",
        "oauth/",
        "healthz",
        "ping",
        "privacy",
        "ws",
    )
    if spa_path.startswith(blocked_prefixes) or spa_path in blocked_prefixes:
        raise HTTPException(status_code=404, detail="Not found")
    file_path = WEB_DIST / spa_path
    if file_path.is_file():
        return FileResponse(file_path)
    return _web_index_response()


app = InstantPingMiddleware(fastapi_app)
