from contextvars import ContextVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from config import get_settings
from services.db import get_stored_refresh_token, persist_refresh_token
from services.user_ids import OWNER_SEED_USER_ID

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_gmail_user_id: ContextVar[str] = ContextVar("gmail_user_id", default=OWNER_SEED_USER_ID)
_cached_credentials: dict[str, Credentials] = {}


def set_gmail_user_context(user_id: str):
    return _gmail_user_id.set(user_id)


def reset_gmail_user_context(token) -> None:
    try:
        _gmail_user_id.reset(token)
    except ValueError:
        # Another asyncio task may have touched this ContextVar (e.g. background watcher).
        _gmail_user_id.set(OWNER_SEED_USER_ID)


def active_gmail_user_id() -> str:
    """The mailbox for this request. A bound user wins over the owner default."""
    from services.mailbox_user import current, user_id

    if current() is not None:
        return user_id()
    return _gmail_user_id.get()


def clear_credentials_cache(user_id: str | None = None) -> None:
    if user_id:
        _cached_credentials.pop(user_id, None)
    else:
        _cached_credentials.clear()


def _effective_refresh_token(user_id: str) -> str:
    settings = get_settings()
    stored = get_stored_refresh_token("gmail", user_id=user_id)
    if stored:
        return stored
    if user_id == OWNER_SEED_USER_ID:
        return settings.gmail_refresh_token
    return ""


def get_credentials(user_id: str | None = None) -> Credentials:
    uid = user_id or active_gmail_user_id()
    settings = get_settings()
    refresh_token = _effective_refresh_token(uid)
    if not refresh_token:
        raise ValueError(f"No Gmail refresh token for user {uid}")

    cached = _cached_credentials.get(uid)
    if cached is None or not cached.valid:
        creds = Credentials(
            token=cached.token if cached else None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            scopes=GMAIL_SCOPES,
        )
        if not creds.valid:
            try:
                creds.refresh(Request())
            except Exception:
                clear_credentials_cache(uid)
                try:
                    from gmail_api.client import clear_gmail_service_cache

                    clear_gmail_service_cache(uid)
                except Exception:
                    pass
                raise
        _cached_credentials[uid] = creds
        return creds
    return cached


def store_refresh_token(refresh_token: str, user_id: str | None = None) -> None:
    """Persist a new refresh token to DB (and clear in-memory cache)."""
    uid = user_id or active_gmail_user_id()
    persist_refresh_token(refresh_token, provider="gmail", user_id=uid)
    clear_credentials_cache(uid)
    try:
        from gmail_api.client import clear_gmail_service_cache

        clear_gmail_service_cache(uid)
    except Exception:
        pass
