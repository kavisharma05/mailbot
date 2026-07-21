from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import get_settings

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_cached_credentials: Credentials | None = None


def get_credentials() -> Credentials:
    global _cached_credentials
    settings = get_settings()
    if _cached_credentials is None or not _cached_credentials.valid:
        creds = Credentials(
            token=_cached_credentials.token if _cached_credentials else None,
            refresh_token=settings.gmail_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            scopes=GMAIL_SCOPES,
        )
        if not creds.valid:
            creds.refresh(Request())
        _cached_credentials = creds
    return _cached_credentials
