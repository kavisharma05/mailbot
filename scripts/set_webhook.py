"""Register the Telegram webhook with your deployed Render URL."""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

if not TOKEN or not SECRET or not RENDER_URL:
    print("Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, and RENDER_URL in .env")
    sys.exit(1)

webhook_url = f"{RENDER_URL}/telegram/webhook/{SECRET}"

resp = httpx.post(
    f"https://api.telegram.org/bot{TOKEN}/setWebhook",
    json={"url": webhook_url, "drop_pending_updates": True},
    timeout=30,
)
data = resp.json()

if data.get("ok"):
    print(f"Webhook set: {webhook_url}")
else:
    print(f"Failed: {data}")
    sys.exit(1)
