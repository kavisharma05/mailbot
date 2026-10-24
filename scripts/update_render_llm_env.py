"""Push LLM-related env vars to the Render mailbot service (requires RENDER_API_KEY in .env)."""

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
        for item in resp.json():
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
        "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY", ""),
        "GEMINI_MODEL": os.getenv("GEMINI_MODEL", ""),
        "GROQ_API_KEY": os.getenv("GROQ_API_KEY", ""),
        "GROQ_MODEL": os.getenv("GROQ_MODEL", ""),
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", ""),
        "OPENROUTER_MODEL": os.getenv("OPENROUTER_MODEL", ""),
        "CEREBRAS_ENABLED": os.getenv("CEREBRAS_ENABLED", ""),
        "NEMOTRON_ENABLED": os.getenv("NEMOTRON_ENABLED", ""),
        "GITHUB_MODELS_ENABLED": os.getenv("GITHUB_MODELS_ENABLED", ""),
        "CLAUDE_SESSION_ENABLED": "false",
    }
    to_push = {k: v for k, v in keys.items() if v}
    if not to_push:
        print("No LLM env vars found in .env to push.")
        sys.exit(1)

    with httpx.Client() as client:
        service_id = find_service_id(client)
        print(f"Updating service {service_id} …")
        for k, v in to_push.items():
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
