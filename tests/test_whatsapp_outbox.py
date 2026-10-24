"""Regressions for async WhatsApp delivery-failure recovery (24h window)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings
from services.messaging import outbox, pending_digest
from services.messaging.whatsapp import (
    WhatsAppClient,
    errors_indicate_outside_window,
)


def _run(coro):
    return asyncio.run(coro)


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
        whatsapp_enabled=True,
        whatsapp_access_token="tok",
        whatsapp_phone_number_id="123",
        whatsapp_owner_phone="1111111111",
    )


def _fake_state(monkeypatch, module):
    store: dict[str, dict] = {}

    async def fake_load(phone):
        return dict(store.get(phone, {}))

    async def fake_save(phone, data):
        store[phone] = dict(data)

    monkeypatch.setattr(module, "load_user_data", fake_load)
    monkeypatch.setattr(module, "save_user_data", fake_save)
    return store


def test_outbox_register_and_take_roundtrip(monkeypatch):
    store = _fake_state(monkeypatch, outbox)
    _run(outbox.register_outbound("111", ["wamid.1", "wamid.2"], "hello alert", kind="alert"))
    assert set(store["111"][outbox.OUTBOX_KEY]) == {"wamid.1", "wamid.2"}

    item = _run(outbox.take_outbound("111", "wamid.1"))
    assert item["text"] == "hello alert"
    assert item["kind"] == "alert"
    # one-shot: second take returns None
    assert _run(outbox.take_outbound("111", "wamid.1")) is None
    assert "wamid.2" in store["111"][outbox.OUTBOX_KEY]


def test_outbox_take_unknown_returns_none(monkeypatch):
    _fake_state(monkeypatch, outbox)
    assert _run(outbox.take_outbound("111", "wamid.nope")) is None


def test_errors_indicate_outside_window():
    assert errors_indicate_outside_window([{"code": 131047, "title": "Re-engagement message"}])
    assert errors_indicate_outside_window([{"code": 131026}])
    assert not errors_indicate_outside_window([{"code": 130000, "title": "other"}])
    assert not errors_indicate_outside_window([])


def test_send_text_registers_wamids(monkeypatch):
    client = WhatsAppClient(_settings())
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"messages": [{"id": "wamid.abc"}]}
    fake_http = MagicMock()
    fake_http.post = AsyncMock(return_value=resp)
    registered = []

    async def fake_register(phone, wamids, text, *, kind="alert"):
        registered.append((phone, wamids, text, kind))

    monkeypatch.setattr("services.messaging.whatsapp.register_outbound", fake_register)

    async def _send():
        with patch("services.messaging.whatsapp._http", return_value=fake_http):
            return await client.send_text("1111111111", "owner alert body", from_html=False)

    assert _run(_send()) is True
    assert registered == [("1111111111", ["wamid.abc"], "owner alert body", "alert")]


def test_stash_appends_to_existing_pending(monkeypatch):
    store = _fake_state(monkeypatch, pending_digest)
    store["111"] = {
        pending_digest.PENDING_KEY: {
            "kind": "morning",
            "text": "old digest",
            "stashed_at": "2026-09-15T01:00:00+00:00",
        }
    }
    _run(pending_digest.stash_pending_digest("111", kind="alert", text="new alert"))
    pending = store["111"][pending_digest.PENDING_KEY]
    assert "old digest" in pending["text"]
    assert "new alert" in pending["text"]
    assert pending["kind"] == "alert"


def test_webhook_failed_status_stashes_and_nudges(monkeypatch):
    import app as app_module

    taken = []
    nudged = []

    async def fake_take(phone, wamid):
        taken.append((phone, wamid))
        return {"text": "full alert text", "kind": "alert"}

    async def fake_nudge(phone, *, kind, text):
        nudged.append((phone, kind, text))
        return True

    monkeypatch.setattr(app_module, "take_outbound", fake_take)
    monkeypatch.setattr(app_module, "stash_and_nudge", fake_nudge)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.fail1",
                                    "status": "failed",
                                    "recipient_id": "917489491188",
                                    "errors": [
                                        {
                                            "code": 131047,
                                            "title": "Re-engagement message",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    _run(app_module._process_whatsapp_payload(payload))
    assert taken == [("917489491188", "wamid.fail1")]
    assert nudged == [("917489491188", "alert", "full alert text")]


def test_webhook_failed_status_without_outbox_match_does_not_nudge(monkeypatch):
    import app as app_module

    nudged = []

    async def fake_take(phone, wamid):
        return None

    async def fake_nudge(phone, *, kind, text):
        nudged.append((phone, kind, text))
        return True

    monkeypatch.setattr(app_module, "take_outbound", fake_take)
    monkeypatch.setattr(app_module, "stash_and_nudge", fake_nudge)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.unknown",
                                    "status": "failed",
                                    "recipient_id": "917489491188",
                                    "errors": [{"code": 131047}],
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    _run(app_module._process_whatsapp_payload(payload))
    assert nudged == []


def test_webhook_non_window_failure_does_not_stash(monkeypatch):
    import app as app_module

    calls = []

    async def fake_take(phone, wamid):
        calls.append((phone, wamid))
        return {"text": "x", "kind": "alert"}

    monkeypatch.setattr(app_module, "take_outbound", fake_take)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.other",
                                    "status": "failed",
                                    "recipient_id": "917489491188",
                                    "errors": [{"code": 130000, "title": "generic"}],
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    _run(app_module._process_whatsapp_payload(payload))
    assert calls == []
