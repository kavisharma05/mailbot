"""Set WHATSAPP_PHONE_MAP on Render mailbot (requires RENDER_API_KEY in .env)."""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

from config import get_settings  # noqa: E402

API = "https://api.render.com/v1"
SERVICE_NAME = "mailbot"
ENV_KEY = "WHATSAPP_PHONE_MAP"


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


def upsert_env(client: httpx.Client, service_id: str, value: str) -> None:
    resp = client.put(
        f"{API}/services/{service_id}/env-vars/{ENV_KEY}",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"value": value},
        timeout=60,
    )
    if resp.status_code == 404:
        resp = client.post(
            f"{API}/services/{service_id}/env-vars",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"envVar": {"key": ENV_KEY, "value": value}},
            timeout=60,
        )
    resp.raise_for_status()


def main() -> None:
    value = (os.getenv(ENV_KEY) or get_settings().whatsapp_phone_map or "").strip()
    if not value:
        print(f"No {ENV_KEY} in env or config defaults")
        sys.exit(1)
    with httpx.Client() as client:
        sid = find_service_id(client)
        upsert_env(client, sid, value)
    print(f"Set {ENV_KEY} on Render service {sid}")


if __name__ == "__main__":
    main()
