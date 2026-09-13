"""Parse Meta WhatsApp webhook payloads."""

from __future__ import annotations

from typing import Any


def extract_inbound_messages(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (from_phone, message_dict) pairs; skip status-only notifications."""
    if payload.get("object") != "whatsapp_business_account":
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if value.get("statuses"):
                continue
            for msg in value.get("messages") or []:
                phone = msg.get("from")
                if phone and isinstance(msg, dict):
                    out.append((str(phone), msg))
    return out
