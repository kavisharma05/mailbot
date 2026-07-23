import hashlib
import asyncio
import logging
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from google.auth.exceptions import RefreshError
from telegram import Update

from bot.telegram_app import build_telegram_application, register_bot_commands
from config import get_settings
from services.db import check_db_health
from services.digest import run_evening_digest, run_morning_digest
from services.formatting import escape_html as _escape_html
from services.oauth_state import create_oauth_state, verify_oauth_state
from services.telegram_dedupe import should_process_update
from services.timeutil import now_ist
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
MORNING_DIGEST_END_HOUR = 9
EVENING_DIGEST_HOUR = 20
EVENING_DIGEST_END_HOUR = 22
PING_BODY = b"ok"

digest_scheduler_lock = asyncio.Lock()


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
    """Fire morning (07:00-09:00 IST) and evening (20:00-22:00 IST) digests while awake."""
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.start()
    await register_bot_commands(ptb_app)
    logger.info("Telegram application started")
    watcher_task = asyncio.create_task(_background_watcher_loop())
    keep_alive_task = asyncio.create_task(_keep_alive_loop())
    digest_task = asyncio.create_task(_digest_scheduler_loop())
    yield
    for task in (watcher_task, keep_alive_task, digest_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("Telegram application stopped")


fastapi_app = FastAPI(title="Mailbot", lifespan=lifespan)


@fastapi_app.get("/healthz")
async def healthz():
    from ai.client import get_llm_status

    db = check_db_health()
    status = "ok" if db.get("ok") else "degraded"
    return {"status": status, "llm": get_llm_status(), "db": db}


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

    result = await run_evening_digest(
        ptb_app.bot, dry_run=bool(dry_run), force=bool(force)
    )
    logger.info("Evening digest result: %s", result)
    return result


@fastapi_app.post("/tasks/run-reminders")
async def run_reminders_task(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    due_result = await run_due_reminders(ptb_app.bot)
    sched_result = await run_scheduled_reminders(ptb_app.bot)
    return {"due_reminders": due_result, "scheduled_reminders": sched_result}


@fastapi_app.get("/oauth/start")
async def oauth_start(token: str | None = Query(default=None)):
    """Redirect owner to Google consent screen for one-tap Gmail reconnect."""
    settings = get_settings()
    expected = hashlib.sha256(f"oauth:{settings.task_secret}".encode()).hexdigest()[:32]
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid OAuth start token")

    redirect_uri = settings.gmail_oauth_redirect_uri
    if not redirect_uri:
        raise HTTPException(
            status_code=503,
            detail="Set GMAIL_OAUTH_REDIRECT_URI to enable one-tap reconnect",
        )
    state = create_oauth_state()
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

    if not verify_oauth_state(state):
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

    store_refresh_token(refresh_token)
    backfill = await run_gmail_reconnect_backfill(ptb_app.bot)
    logger.info("Gmail reconnected; backfill: %s", backfill)
    return HTMLResponse(
        "<h1>Gmail reconnected</h1><p>You can close this tab and return to Telegram.</p>"
    )


app = InstantPingMiddleware(fastapi_app)
