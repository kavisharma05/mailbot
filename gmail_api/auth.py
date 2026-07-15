from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from config import get_settings

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]


def get_credentials() -> Credentials:
    settings = get_settings()
    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        scopes=GMAIL_SCOPES,
    )
    if not creds.valid:
        creds.refresh(Request())
    return creds
