"""Push Gmail OAuth env vars to the Render mailbot service (requires RENDER_API_KEY in .env).

Create an API key: Render Dashboard → Account Settings → API Keys.
Add to .env: RENDER_API_KEY=rnd_...
Optional: RENDER_SERVICE_ID=srv-... (auto-discovered by name if omitted)
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

API = "https://api.render.com/v1"
SERVICE_NAME = "mailbot"


def _headers() -> dict[str, str]:
    key = os.getenv("RENDER_API_KEY", "").strip()
    if not key:
        print("Set RENDER_API_KEY in .env (Render → Account Settings → API Keys)")
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
        data = resp.json()
        for item in data:
            svc = item.get("service") or item
            if svc.get("name") == SERVICE_NAME:
                return svc["id"]
        cursor = resp.headers.get("X-Next-Cursor") or resp.headers.get("x-next-cursor")
        if not cursor:
            break
    print(f"Could not find Render service named {SERVICE_NAME!r}. Set RENDER_SERVICE_ID.")
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
    keys = {
        "GMAIL_CLIENT_ID": os.getenv("GMAIL_CLIENT_ID", ""),
        "GMAIL_CLIENT_SECRET": os.getenv("GMAIL_CLIENT_SECRET", ""),
        "GMAIL_OAUTH_REDIRECT_URI": os.getenv("GMAIL_OAUTH_REDIRECT_URI", ""),
        "RENDER_URL": os.getenv("RENDER_URL", ""),
    }
    missing = [k for k, v in keys.items() if not v]
    if missing:
        print("Missing in .env:", ", ".join(missing))
        sys.exit(1)

    with httpx.Client() as client:
        service_id = find_service_id(client)
        print(f"Updating service {service_id} …")
        for k, v in keys.items():
            upsert_env(client, service_id, k, v)
            print(f"  set {k}")
        deploy = client.post(
            f"{API}/services/{service_id}/deploys",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"clearCache": "do_not_clear"},
            timeout=60,
        )
        if deploy.status_code < 400:
            print("Triggered redeploy.")
        else:
            print("Env updated. Redeploy from Render dashboard if needed.", deploy.text[:200])


if __name__ == "__main__":
    main()
