import asyncio
import logging
import hmac
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from telegram import Update

from bot.telegram_app import build_telegram_application, register_bot_commands
from config import get_settings
from services.db import check_db_health
from services.digest import run_evening_digest, run_morning_digest
from services.watcher import (
    WATCHER_INTERVAL_SECONDS,
    run_gmail_backfill,
    run_gmail_watcher,
    watcher_lock,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ptb_app = build_telegram_application()

# Render free tier sleeps after ~15 min of no inbound HTTP traffic.
# Ping well inside that window and retry quickly so a single slow/failed
# request can never open a >15 min gap.
KEEP_ALIVE_INTERVAL_SECONDS = 10 * 60
KEEP_ALIVE_RETRY_SECONDS = 30
KEEP_ALIVE_MAX_RETRIES = 3
PING_BODY = b"ok"


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
    while True:
        try:
            async with watcher_lock:
                result = await run_gmail_watcher(ptb_app.bot)
            logger.info("Background watcher result: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Background watcher failed: %s", exc)
        await asyncio.sleep(WATCHER_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.start()
    await register_bot_commands(ptb_app)
    logger.info("Telegram application started")
    watcher_task = asyncio.create_task(_background_watcher_loop())
    keep_alive_task = asyncio.create_task(_keep_alive_loop())
    yield
    for task in (watcher_task, keep_alive_task):
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
    await ptb_app.process_update(update)
    return Response(status_code=200)


@fastapi_app.post("/tasks/watch-gmail")
async def watch_gmail(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    async with watcher_lock:
        result = await run_gmail_watcher(ptb_app.bot)
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

    result = await run_gmail_backfill(ptb_app.bot, days=days)
    logger.info("Gmail backfill result: %s", result)
    return result


@fastapi_app.post("/tasks/digest-morning")
async def digest_morning(
    dry_run: int = 0,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    result = await run_morning_digest(ptb_app.bot, dry_run=bool(dry_run))
    logger.info("Morning digest result: %s", result)
    return result


@fastapi_app.post("/tasks/digest-evening")
async def digest_evening(
    dry_run: int = 0,
    x_task_secret: str = Header(..., alias="x-task-secret"),
):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    result = await run_evening_digest(ptb_app.bot, dry_run=bool(dry_run))
    logger.info("Evening digest result: %s", result)
    return result


app = InstantPingMiddleware(fastapi_app)
