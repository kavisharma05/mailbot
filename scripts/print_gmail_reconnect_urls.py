"""Print one-tap Gmail reconnect URLs for the owner and each active student."""

from __future__ import annotations

import hashlib

from config import get_settings
from services.oauth_state import create_oauth_state
from services.user_ids import OWNER_SEED_USER_ID
from services.users import ensure_owner_user, list_users


def _base_url() -> str:
    settings = get_settings()
    base = (settings.render_url or settings.gmail_oauth_redirect_uri or "").rstrip("/")
    if not base or "your-app" in base:
        raise SystemExit("Set RENDER_URL or GMAIL_OAUTH_REDIRECT_URI in .env")
    if settings.gmail_oauth_redirect_uri:
        from urllib.parse import urlparse

        parsed = urlparse(settings.gmail_oauth_redirect_uri)
        if parsed.scheme and parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
    return base


def owner_url(base: str) -> str:
    settings = get_settings()
    token = hashlib.sha256(f"oauth:{settings.task_secret}".encode()).hexdigest()[:32]
    return f"{base}/oauth/start?token={token}"


def user_url(base: str, user_id: str) -> str:
    state = create_oauth_state(user_id)
    return f"{base}/oauth/start?s={state}"


def main() -> None:
    ensure_owner_user()
    base = _base_url()
    print("Gmail reconnect (open in browser while logged into the right Google account):\n")
    print(f"Owner: {owner_url(base)}")
    for row in list_users(active_only=True):
        uid = str(row["id"])
        if uid == OWNER_SEED_USER_ID:
            continue
        label = row.get("display_name") or row.get("gmail_address") or uid
        print(f"{label}: {user_url(base, uid)}")
    print(
        "\nAfter reconnect, publish the OAuth consent screen to Production in Google Cloud "
        "so refresh tokens stop expiring every 7 days (Testing mode limit)."
    )


if __name__ == "__main__":
    main()
