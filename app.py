import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from telegram import Update

from bot.telegram_app import build_telegram_application
from config import get_settings
from services.watcher import run_gmail_watcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ptb_app = build_telegram_application()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ptb_app.initialize()
    await ptb_app.start()
    logger.info("Telegram application started")
    yield
    await ptb_app.stop()
    await ptb_app.shutdown()
    logger.info("Telegram application stopped")


app = FastAPI(title="Mailbot", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post(f"/telegram/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request) -> Response:
    settings = get_settings()
    if secret != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=200)


@app.post("/tasks/watch-gmail")
async def watch_gmail(x_task_secret: str = Header(..., alias="x-task-secret")):
    settings = get_settings()
    if x_task_secret != settings.task_secret:
        raise HTTPException(status_code=403, detail="Invalid task secret")

    result = await run_gmail_watcher(ptb_app.bot)
    logger.info("Gmail watcher result: %s", result)
    return result
