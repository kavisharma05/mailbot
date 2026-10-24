"""Set WhatsApp template env vars on Render mailbot (requires RENDER_API_KEY in .env)."""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

API = "https://api.render.com/v1"
SERVICE_NAME = "mailbot"

DEFAULTS = {
    "WHATSAPP_ENABLED": "true",
    "WHATSAPP_ALERT_TEMPLATE": "mailbot_alert_v1",
    "WHATSAPP_DIGEST_TEMPLATE": "mailbot_digest_v1",
}


def _headers() -> dict[str, str]:
    key = os.getenv("RENDER_API_KEY", "").strip()
    if not key:
        print("Set RENDER_API_KEY in .env")
        sys.exit(1)
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def find_service_id(client: httpx.Client) -> str:
    sid = os.getenv("RENDER_SERVICE_ID", "").strip()
    if sid:
        return sid
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(f"{API}/services", headers=_headers(), params=params, timeout=60)
        resp.raise_for_status()
        for item in resp.json():
            svc = item.get("service") or item
            if svc.get("name") == SERVICE_NAME:
                return svc["id"]
        cursor = resp.headers.get("X-Next-Cursor") or resp.headers.get("x-next-cursor")
        if not cursor:
            break
    print(f"Could not find Render service {SERVICE_NAME!r}. Set RENDER_SERVICE_ID.")
    sys.exit(1)


def upsert_env(client: httpx.Client, service_id: str, key: str, value: str) -> None:
    resp = client.put(
        f"{API}/services/{service_id}/env-vars/{key}",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"value": value},
        timeout=60,
    )
    if resp.status_code == 404:
        resp = client.post(
            f"{API}/services/{service_id}/env-vars",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"envVar": {"key": key, "value": value}},
            timeout=60,
        )
    resp.raise_for_status()


def main() -> None:
    with httpx.Client() as client:
        sid = find_service_id(client)
        for key, default in DEFAULTS.items():
            value = (os.getenv(key) or default).strip()
            upsert_env(client, sid, key, value)
            print(f"Set {key}={value!r}")
    print(f"Updated WhatsApp env on Render service {sid} (redeploy may be required)")


if __name__ == "__main__":
    main()
