"""WhatsApp digest: stash when the 24h window is closed, flush on reply."""

import asyncio

import pytest

from services.messaging.pending_digest import (
    DIGEST_KEYWORDS,
    PENDING_KEY,
    deliver_whatsapp_digest,
    flush_pending_digest,
    take_pending_digest,
)


def _run(coro):
    return asyncio.run(coro)


def test_digest_keywords_include_plain_digest():
    assert "digest" in DIGEST_KEYWORDS
    assert "/digest" in DIGEST_KEYWORDS


def test_take_pending_digest_pops():
    data = {PENDING_KEY: {"kind": "morning", "text": "hello"}, "other": 1}
    pending = take_pending_digest(data)
    assert pending["text"] == "hello"
    assert PENDING_KEY not in data
    assert data["other"] == 1


def test_deliver_stashes_when_freeform_fails(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_ALERT_TEMPLATE", "mailbot_alert_v1")
    from config import get_settings

    get_settings.cache_clear()

    sent: dict[str, object] = {}

    class FakeClient:
        enabled = True

        async def send_text(self, *a, **k):
            return False

        async def send_template(self, to, name, body_parameter=None, **k):
            sent["template"] = (to, name, body_parameter)
            return True

    stored: dict = {}

    async def fake_load(_phone):
        return dict(stored)

    async def fake_save(_phone, data):
        stored.clear()
        stored.update(data)

    monkeypatch.setattr("services.messaging.pending_digest.get_whatsapp_client", lambda: FakeClient())
    monkeypatch.setattr("services.messaging.pending_digest.load_user_data", fake_load)
    monkeypatch.setattr("services.messaging.pending_digest.save_user_data", fake_save)

    result = _run(deliver_whatsapp_digest("<b>Morning</b> due: 2", kind="morning"))
    assert result == "stashed"
    assert PENDING_KEY in stored
    assert "Morning digest ready" in str(sent["template"][2])


def test_deliver_uses_static_digest_template_without_body_param(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    monkeypatch.setenv("WHATSAPP_DIGEST_TEMPLATE", "mailbot_digest_v1")
    monkeypatch.setenv("WHATSAPP_ALERT_TEMPLATE", "mailbot_alert_v1")
    from config import get_settings

    get_settings.cache_clear()

    sent: dict[str, object] = {}

    class FakeClient:
        enabled = True

        async def send_text(self, *a, **k):
            return False

        async def send_template(self, to, name, body_parameter=None, **k):
            sent["template"] = (to, name, body_parameter)
            return True

    stored: dict = {}

    async def fake_load(_phone):
        return dict(stored)

    async def fake_save(_phone, data):
        stored.clear()
        stored.update(data)

    monkeypatch.setattr("services.messaging.pending_digest.get_whatsapp_client", lambda: FakeClient())
    monkeypatch.setattr("services.messaging.pending_digest.load_user_data", fake_load)
    monkeypatch.setattr("services.messaging.pending_digest.save_user_data", fake_save)

    result = _run(deliver_whatsapp_digest("due: 2", kind="evening"))
    assert result == "stashed"
    assert sent["template"] == ("1111111111", "mailbot_digest_v1", None)


def test_flush_sends_stashed_digest(monkeypatch):
    class FakeClient:
        enabled = True
        sent: list[str] = []

        async def send_text(self, phone, text, **k):
            self.sent.append(text)
            return True

    client = FakeClient()
    monkeypatch.setattr("services.messaging.pending_digest.get_whatsapp_client", lambda: client)
    user_data = {PENDING_KEY: {"kind": "evening", "text": "full evening digest"}}
    ok = _run(flush_pending_digest("1111111111", user_data))
    assert ok is True
    assert client.sent == ["full evening digest"]
    assert PENDING_KEY not in user_data


def test_router_does_not_flush_pending_on_menu(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    from config import get_settings

    get_settings.cache_clear()
    from bot import whatsapp_router

    flushed: list[str] = []

    async def fake_flush(phone, user_data=None):
        flushed.append(phone)
        return True

    async def fake_load(_phone):
        return {}

    async def fake_save(_phone, _data):
        return None

    async def fake_menu(phone, *, style="menu"):
        return True

    monkeypatch.setattr(whatsapp_router, "flush_pending_digest", fake_flush)
    monkeypatch.setattr(whatsapp_router, "load_user_data", fake_load)
    monkeypatch.setattr(whatsapp_router, "save_user_data", fake_save)
    monkeypatch.setattr(whatsapp_router, "send_main_menu", fake_menu)

    _run(
        whatsapp_router.process_whatsapp_message(
            "1111111111",
            {"id": "m1", "type": "text", "text": {"body": "menu"}},
        )
    )
    assert flushed == []


def test_router_flushes_pending_on_digest_keyword(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    from config import get_settings

    get_settings.cache_clear()
    from bot import whatsapp_router

    flushed: list[str] = []

    async def fake_flush(phone, user_data=None):
        flushed.append(phone)
        if user_data is not None:
            user_data.pop("pending_whatsapp_digest", None)
        return True

    async def fake_load(_phone):
        return {}

    async def fake_save(_phone, _data):
        return None

    monkeypatch.setattr(whatsapp_router, "flush_pending_digest", fake_flush)
    monkeypatch.setattr(whatsapp_router, "load_user_data", fake_load)
    monkeypatch.setattr(whatsapp_router, "save_user_data", fake_save)

    _run(
        whatsapp_router.process_whatsapp_message(
            "1111111111",
            {"id": "m2", "type": "text", "text": {"body": "digest"}},
        )
    )
    assert flushed == ["1111111111"]
