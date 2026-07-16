import base64
import logging
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from googleapiclient.discovery import build

from gmail_api.auth import get_credentials

logger = logging.getLogger(__name__)


def get_gmail_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_profile_history_id() -> str:
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def list_history_message_ids(start_history_id: str) -> tuple[list[str], str | None]:
    """Return new message IDs since start_history_id and the latest historyId."""
    service = get_gmail_service()
    message_ids: list[str] = []
    latest_history_id: str | None = None
    page_token = None

    try:
        while True:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token

            response = service.users().history().list(**kwargs).execute()
            latest_history_id = str(response.get("historyId", latest_history_id or start_history_id))

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels or "SPAM" in labels:
                        message_ids.append(msg["id"])

            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.warning("History list failed (%s), falling back to recent messages", exc)
        return list_recent_inbox_message_ids(), None

    return list(dict.fromkeys(message_ids)), latest_history_id


def list_recent_inbox_message_ids() -> list[str]:
    return list_inbox_message_ids_since(days=1, max_results=50)


def list_inbox_message_ids_since(days: int = 14, max_results: int = 200) -> list[str]:
    """List inbox and spam message IDs from the last N days, paginated up to max_results."""
    service = get_gmail_service()
    # Spam is excluded from searches by default; include it explicitly.
    query = f"(in:inbox OR in:spam) newer_than:{days}d"
    message_ids: list[str] = []
    page_token = None

    while len(message_ids) < max_results:
        page_size = min(100, max_results - len(message_ids))
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": page_size,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        for message in response.get("messages", []):
            message_ids.append(message["id"])
            if len(message_ids) >= max_results:
                break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return list(dict.fromkeys(message_ids))


def search_message_ids(query: str, max_results: int = 8) -> list[str]:
    """Search the whole mailbox (incl. spam) for a Gmail query string."""
    service = get_gmail_service()
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results, includeSpamTrash=True)
        .execute()
    )
    return [m["id"] for m in response.get("messages", [])]


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_text_from_payload(payload: dict[str, Any]) -> str:
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and body_data:
        return _decode_body_data(body_data)

    parts = payload.get("parts", [])
    plain_chunks: list[str] = []
    html_chunks: list[str] = []

    for part in parts:
        part_type = part.get("mimeType", "")
        if part_type == "text/plain":
            data = part.get("body", {}).get("data")
            if data:
                plain_chunks.append(_decode_body_data(data))
        elif part_type == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                html_chunks.append(_decode_body_data(data))
        elif part.get("parts"):
            nested = _extract_text_from_payload(part)
            if nested:
                plain_chunks.append(nested)

    if plain_chunks:
        return "\n".join(plain_chunks)

    if html_chunks:
        html = html_chunks[0]
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()

    return ""


def get_message_details(message_id: str) -> dict[str, str]:
    service = get_gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "(no subject)")
    sender = headers.get("from", "")
    body = _extract_text_from_payload(msg.get("payload", {}))

    received_at = ""
    received_at_iso = ""
    internal_ms = msg.get("internalDate")
    if internal_ms:
        try:
            dt = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
            # Store ISO (UTC) for relative deadline anchoring; display in IST
            received_at_iso = dt.isoformat()
            from zoneinfo import ZoneInfo

            received_at = dt.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %I:%M %p IST")
        except (TypeError, ValueError, OSError):
            received_at = headers.get("date", "")

    return {
        "message_id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": subject,
        "sender": sender,
        "received_at": received_at,
        "received_at_iso": received_at_iso,
        "body": body[:8000],
    }


def send_email(to: str, subject: str, body: str, thread_id: str | None = None, from_addr: str | None = None) -> dict[str, str]:
    from config import get_settings

    service = get_gmail_service()
    settings = get_settings()
    mime = MIMEText(body)
    mime["to"] = to
    mime["from"] = from_addr or settings.gmail_user
    mime["subject"] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id

    sent = service.users().messages().send(userId="me", body=payload).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId", "")}
