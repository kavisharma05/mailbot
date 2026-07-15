"""Register the Telegram webhook with your deployed Render URL."""

import os
import sys

import certifi
import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

if not TOKEN or not SECRET or not RENDER_URL:
    print("Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, and RENDER_URL in .env")
    sys.exit(1)

if "your-app.onrender.com" in RENDER_URL:
    print("RENDER_URL still looks like a placeholder. Set it to your real Render URL.")
    sys.exit(1)

webhook_url = f"{RENDER_URL}/telegram/webhook/{SECRET}"
api_url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
payload = {"url": webhook_url, "drop_pending_updates": True}


def _post(verify: str | bool) -> dict:
    resp = httpx.post(api_url, json=payload, timeout=30, verify=verify)
    return resp.json()


try:
    data = _post(certifi.where())
except httpx.ConnectError as exc:
    if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
        raise
    # Common on some Windows Python installs / antivirus TLS interception.
    print("SSL verify failed locally; retrying once with verify disabled (local script only).")
    data = _post(False)

if data.get("ok"):
    print(f"Webhook set: {webhook_url}")
else:
    print(f"Failed: {data}")
    sys.exit(1)
