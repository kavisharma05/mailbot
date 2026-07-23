from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import get_settings
from services.db import get_stored_refresh_token, persist_refresh_token

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_cached_credentials: Credentials | None = None


def clear_credentials_cache() -> None:
    global _cached_credentials
    _cached_credentials = None


def _effective_refresh_token() -> str:
    settings = get_settings()
    stored = get_stored_refresh_token("gmail")
    if stored:
        return stored
    return settings.gmail_refresh_token


def get_credentials() -> Credentials:
    global _cached_credentials
    settings = get_settings()
    refresh_token = _effective_refresh_token()
    if _cached_credentials is None or not _cached_credentials.valid:
        creds = Credentials(
            token=_cached_credentials.token if _cached_credentials else None,
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
                # Drop bad cache so a rotated refresh token can be picked up.
                clear_credentials_cache()
                try:
                    from gmail_api.client import clear_gmail_service_cache

                    clear_gmail_service_cache()
                except Exception:
                    pass
                raise
        _cached_credentials = creds
    return _cached_credentials


def store_refresh_token(refresh_token: str) -> None:
    """Persist a new refresh token to DB (and clear in-memory cache)."""
    persist_refresh_token(refresh_token, provider="gmail")
    clear_credentials_cache()
    try:
        from gmail_api.client import clear_gmail_service_cache

        clear_gmail_service_cache()
    except Exception:
        pass
