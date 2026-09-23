import base64
import logging
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gmail_api.auth import active_gmail_user_id, get_credentials

logger = logging.getLogger(__name__)

_gmail_services: dict[str, Any] = {}


def clear_gmail_service_cache(user_id: str | None = None) -> None:
    if user_id:
        _gmail_services.pop(user_id, None)
    else:
        _gmail_services.clear()


def get_gmail_service(user_id: str | None = None):
    uid = user_id or active_gmail_user_id()
    if uid not in _gmail_services:
        creds = get_credentials(uid)
        _gmail_services[uid] = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _gmail_services[uid]


def get_profile_history_id() -> str:
    service = get_gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def list_history_message_ids(start_history_id: str) -> tuple[list[str], str | None, bool]:
    """Return (message_ids, latest_history_id, history_api_failed)."""
    message_ids: list[str] = []
    latest_history_id: str | None = None
    page_token = None

    try:
        # Keep service construction inside try so auth / history-id errors can fall back.
        service = get_gmail_service()
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
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    labels = msg.get("labelIds") or []
                    if not labels or "INBOX" in labels or "SPAM" in labels:
                        message_ids.append(msg_id)

            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except RefreshError:
        clear_gmail_service_cache()
        raise
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 404:
            # startHistoryId is stale/expired — Gmail keeps history for a
            # limited window. Returning the CURRENT profile historyId lets the
            # watcher re-anchor instead of retrying the dead id forever.
            logger.warning(
                "startHistoryId %s is stale (404) — resetting to current historyId",
                start_history_id,
            )
            fresh_id: str | None = None
            try:
                fresh_id = get_profile_history_id()
            except Exception as profile_exc:
                logger.warning("Could not fetch fresh historyId: %s", profile_exc)
            return list_recent_inbox_message_ids(), fresh_id, True
        logger.warning("History list failed (%s), falling back to recent messages", exc)
        return list_recent_inbox_message_ids(), None, True
    except Exception as exc:
        logger.warning("History list failed (%s), falling back to recent messages", exc)
        return list_recent_inbox_message_ids(), None, True

    return list(dict.fromkeys(message_ids)), latest_history_id, False


def list_recent_inbox_message_ids(*, days: int = 14, max_results: int = 300) -> list[str]:
    return list_inbox_message_ids_since(days=days, max_results=max_results)


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


def list_message_senders(query: str, *, max_results: int = 200) -> list[dict[str, Any]]:
    """Lightweight From-header fetch for contact backfill (metadata only)."""
    service = get_gmail_service()
    message_ids: list[str] = []
    page_token = None

    while len(message_ids) < max_results:
        page_size = min(100, max_results - len(message_ids))
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": page_size,
            "includeSpamTrash": True,
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

    senders: list[dict[str, Any]] = []
    for message_id in message_ids:
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
                .execute()
            )
        except Exception as exc:
            logger.warning("Failed loading sender metadata for %s: %s", message_id, exc)
            continue
        headers = {
            h["name"].lower(): h["value"]
            for h in (msg.get("payload") or {}).get("headers", [])
        }
        senders.append({"message_id": message_id, "sender": headers.get("from", "")})
    return senders


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


def count_messages(query: str, *, hard_cap: int = 500) -> dict[str, Any]:
    """Count messages matching a Gmail query (paginated, capped)."""
    service = get_gmail_service()
    total = 0
    page_token = None
    estimate = None
    while total < hard_cap:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": min(100, hard_cap - total),
            "includeSpamTrash": True,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = service.users().messages().list(**kwargs).execute()
        if estimate is None:
            estimate = response.get("resultSizeEstimate")
        batch = response.get("messages") or []
        total += len(batch)
        page_token = response.get("nextPageToken")
        if not page_token or not batch:
            break
    return {
        "query": query,
        "count": total,
        "capped": bool(page_token),
        "estimate": estimate,
    }


def _message_metadata_summary(message_id: str) -> dict[str, Any] | None:
    """Headers-only Gmail fetch — much cheaper than full payload + body decode."""
    service = get_gmail_service()
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
            )
            .execute()
        )
    except Exception as exc:
        logger.warning("Failed loading message metadata %s: %s", message_id, exc)
        return None
    headers = {
        h["name"].lower(): h["value"]
        for h in (msg.get("payload") or {}).get("headers", [])
    }
    return {
        "message_id": message_id,
        "thread_id": msg.get("threadId"),
        "subject": headers.get("subject"),
        "sender": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "received_at": headers.get("date"),
    }


def list_message_summaries(
    query: str,
    *,
    max_results: int = 40,
    include_body: bool = False,
    body_chars: int = 1200,
    load_attachments: bool = False,
    attachment_chars: int = 6000,
) -> list[dict[str, Any]]:
    """List recent matching messages with sender/subject/time (optional body/attachments)."""
    ids = search_message_ids(query, max_results=max_results)
    if not include_body and not load_attachments:
        summaries = []
        for message_id in ids:
            item = _message_metadata_summary(message_id)
            if item:
                summaries.append(item)
        return summaries

    summaries: list[dict[str, Any]] = []
    for message_id in ids:
        try:
            details = get_message_details(message_id, load_attachments=load_attachments)
        except Exception as exc:
            logger.warning("Failed loading message %s: %s", message_id, exc)
            continue
        item: dict[str, Any] = {
            "message_id": details.get("message_id"),
            "thread_id": details.get("thread_id"),
            "subject": details.get("subject"),
            "sender": details.get("sender"),
            "to": details.get("to"),
            "cc": details.get("cc"),
            "received_at": details.get("received_at"),
        }
        if include_body:
            item["body"] = (details.get("body") or "")[:body_chars]
        if load_attachments:
            attachments = details.get("attachments") or []
            if attachments:
                item["attachment_filenames"] = [
                    a.get("filename") for a in attachments if a.get("filename")
                ]
            attach_text = (details.get("attachment_text") or "").strip()
            if attach_text:
                item["attachment_text"] = attach_text[:attachment_chars]
        summaries.append(item)
    return summaries


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


def _walk_attachment_parts(payload: dict[str, Any], out: list[dict[str, Any]]) -> None:
    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    att_id = body.get("attachmentId")
    size = int(body.get("size") or 0)
    mime = payload.get("mimeType") or "application/octet-stream"
    if filename and att_id:
        out.append(
            {
                "filename": filename,
                "mime_type": mime,
                "attachment_id": att_id,
                "size": size,
            }
        )
    for part in payload.get("parts") or []:
        _walk_attachment_parts(part, out)


def list_message_attachments(message_id: str) -> list[dict[str, Any]]:
    service = get_gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    attachments: list[dict[str, Any]] = []
    _walk_attachment_parts(msg.get("payload") or {}, attachments)
    return attachments


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    service = get_gmail_service()
    att = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = att.get("data") or ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def get_message_details(message_id: str, *, load_attachments: bool = True) -> dict[str, Any]:
    service = get_gmail_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("subject", "(no subject)")
    sender = headers.get("from", "")
    to_header = headers.get("to", "")
    cc_header = headers.get("cc", "")
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

    attachments: list[dict[str, Any]] = []
    attachment_text = ""
    if load_attachments:
        from services.attachments import (
            combine_attachment_text,
            extract_text_from_bytes,
            should_forward_attachment,
        )

        meta = []
        _walk_attachment_parts(msg.get("payload") or {}, meta)
        for item in meta[:8]:
            try:
                raw = download_attachment(message_id, item["attachment_id"])
            except Exception as exc:
                logger.warning("Attachment download failed %s: %s", item.get("filename"), exc)
                continue
            item = {**item, "data": raw, "size": len(raw)}
            item["extracted_text"] = extract_text_from_bytes(
                item["filename"], item["mime_type"], raw
            )
            item["forward"] = should_forward_attachment(item)
            attachments.append(item)
        attachment_text = combine_attachment_text(attachments)

    return {
        "message_id": message_id,
        "thread_id": msg.get("threadId", ""),
        "subject": subject,
        "sender": sender,
        "to": to_header,
        "cc": cc_header,
        "received_at": received_at,
        "received_at_iso": received_at_iso,
        "body": body[:8000],
        "attachments": attachments,
        "attachment_text": attachment_text,
        "label_ids": msg.get("labelIds") or [],
    }


def get_thread_message_details(
    message_id: str,
    *,
    max_messages: int = 6,
    load_attachments: bool = False,
) -> list[dict[str, Any]]:
    """Return chronological details for the Gmail thread containing message_id.

    Gmail search returns individual messages, and sender-filtered queries often match
    only the first inbound mail. Reading the thread ensures reply context and the
    latest message are available for summaries.
    """
    service = get_gmail_service()
    seed = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata")
        .execute()
    )
    thread_id = seed.get("threadId")
    if not thread_id:
        return [get_message_details(message_id, load_attachments=load_attachments)]

    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="metadata")
        .execute()
    )
    messages = list(thread.get("messages") or [])
    messages.sort(key=lambda m: int(m.get("internalDate") or 0))
    selected = messages[-max_messages:] if max_messages > 0 else messages
    details: list[dict[str, Any]] = []
    for idx, item in enumerate(selected):
        mid = item.get("id")
        if not mid:
            continue
        is_latest = idx == len(selected) - 1
        try:
            details.append(
                get_message_details(
                    str(mid),
                    load_attachments=load_attachments and is_latest,
                )
            )
        except Exception as exc:
            logger.warning("Thread message fetch failed for %s: %s", mid, exc)
    return details


def primary_mailbox_display_name(user_id: str | None = None) -> str | None:
    """Name shown on the Gmail account (Send mail as), not the Telegram handle."""
    service = get_gmail_service(user_id)
    listed = service.users().settings().sendAs().list(userId="me").execute()
    aliases = listed.get("sendAs") or []
    primary = next((row for row in aliases if row.get("isPrimary")), None)
    chosen = primary or (aliases[0] if aliases else None)
    if not chosen:
        return None
    name = str(chosen.get("displayName") or "").strip()
    return name or None


def send_email(to: str, subject: str, body: str, thread_id: str | None = None, from_addr: str | None = None) -> dict[str, str]:
    from config import get_settings
    from services.mailbox_user import current, gmail_address

    service = get_gmail_service()
    settings = get_settings()
    sender = (from_addr or "").strip() or gmail_address()
    if not sender and current() is None:
        sender = (settings.gmail_user or "").strip()
    mime = MIMEText(body)
    mime["to"] = to
    if sender:
        mime["from"] = sender
    mime["subject"] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    payload: dict[str, Any] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id

    sent = service.users().messages().send(userId="me", body=payload).execute()
    return {"message_id": sent["id"], "thread_id": sent.get("threadId", "")}
