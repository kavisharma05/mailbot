import asyncio
import logging
import hmac
from contextlib import asynccontextmanager

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
    yield
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("Telegram application stopped")


app = FastAPI(title="Mailbot", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    from ai.client import get_llm_status

    db = check_db_health()
    status = "ok" if db.get("ok") else "degraded"
    return {"status": status, "llm": get_llm_status(), "db": db}


@app.post(f"/telegram/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    settings = get_settings()
    if not hmac.compare_digest(secret, settings.telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)


@app.post("/tasks/watch-gmail")
async def watch_gmail(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if not hmac.compare_digest(x_task_secret, settings.task_secret):
        raise HTTPException(status_code=403, detail="Invalid task secret")

    async with watcher_lock:
        result = await run_gmail_watcher(ptb_app.bot)
    logger.info("Gmail watcher result: %s", result)
    return result


@app.post("/tasks/backfill")
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


@app.post("/tasks/digest-morning")
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


@app.post("/tasks/digest-evening")
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
