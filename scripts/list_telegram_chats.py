"""List Telegram chats seen in getUpdates (for finding group chat IDs)."""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import get_settings


def main() -> None:
    settings = get_settings()
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates"
    resp = httpx.get(url, params={"limit": 100}, timeout=30, verify=False)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print("API error:", data)
        return

    chats: dict[int, dict] = {}
    for update in data.get("result", []):
        chat = None
        if update.get("message"):
            chat = update["message"].get("chat")
        elif update.get("my_chat_member"):
            chat = update["my_chat_member"].get("chat")
        elif update.get("callback_query"):
            chat = update["callback_query"].get("message", {}).get("chat")
        if not chat or chat.get("id") is None:
            continue
        cid = chat["id"]
        if cid not in chats:
            chats[cid] = {
                "id": cid,
                "type": chat.get("type"),
                "title": chat.get("title"),
                "username": chat.get("username"),
            }

    print(f"Owner chat ID (from env): {settings.telegram_owner_chat_id}")
    print(f"Placements group (env): {settings.telegram_chat_placements or '(not set)'}")
    print(f"Certification group (env): {settings.telegram_chat_certification or '(not set)'}")
    print()
    print("Chats seen in getUpdates:")
    print("-" * 72)
    for c in sorted(chats.values(), key=lambda x: (x.get("type") or "", x.get("title") or "")):
        title = c.get("title") or c.get("username") or "(no title)"
        marker = ""
        tlower = title.lower()
        if "cert" in tlower or "imt cert" in tlower:
            marker = "  <-- likely CERTIFICATION group (imt certification)"
        elif "placement" in tlower:
            marker = "  <-- likely PLACEMENTS group"
        elif "deadline" in tlower:
            marker = "  <-- likely DEADLINES group"
        elif "admin" in tlower:
            marker = "  <-- likely ADMIN group"
        print(f"{c['id']}\t{c.get('type')}\t{title}{marker}")


if __name__ == "__main__":
    main()
