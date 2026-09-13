"""Tests for the WhatsApp interactive menu, router dispatch, and typing indicator."""

import asyncio

import pytest

from bot.whatsapp_menu import CMD_PREFIX, MENU_KEYWORDS, main_menu_interactive
from services.messaging.format import MAX_BUTTON_TITLE, MAX_LIST_ROW_TITLE


def test_menu_payload_within_cloud_api_limits():
    payload = main_menu_interactive("menu")
    assert payload["type"] == "list"
    action = payload["action"]
    assert len(action["button"]) <= MAX_BUTTON_TITLE
    rows = action["sections"][0]["rows"]
    assert 1 <= len(rows) <= 10
    for row in rows:
        assert row["id"].startswith(CMD_PREFIX)
        assert len(row["title"]) <= MAX_LIST_ROW_TITLE
        assert len(row.get("description", "")) <= 72
    assert len(payload["body"]["text"]) <= 1024


def test_menu_styles_all_have_bodies():
    for style in ("welcome", "help", "menu", "unknown"):
        payload = main_menu_interactive(style)
        assert payload["body"]["text"].strip()


def test_menu_keywords_are_lowercase():
    assert "menu" in MENU_KEYWORDS
    assert all(k == k.lower() for k in MENU_KEYWORDS)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def router_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    from config import get_settings

    get_settings.cache_clear()
    from bot import whatsapp_router

    async def fake_load(_phone):
        return {}

    async def fake_save(_phone, _data):
        return None

    monkeypatch.setattr(whatsapp_router, "load_user_data", fake_load)
    monkeypatch.setattr(whatsapp_router, "save_user_data", fake_save)
    return whatsapp_router


def test_router_menu_keyword_sends_menu(router_env, monkeypatch):
    sent: list[str] = []

    async def fake_menu(phone, *, style="menu"):
        sent.append(style)
        return True

    monkeypatch.setattr(router_env, "send_main_menu", fake_menu)
    _run(
        router_env.process_whatsapp_message(
            "1111111111",
            {"id": "m1", "type": "text", "text": {"body": "Menu"}},
        )
    )
    assert sent == ["menu"]


def test_router_start_sends_welcome_menu(router_env, monkeypatch):
    sent: list[str] = []

    async def fake_menu(phone, *, style="menu"):
        sent.append(style)
        return True

    monkeypatch.setattr(router_env, "send_main_menu", fake_menu)
    _run(
        router_env.process_whatsapp_message(
            "1111111111",
            {"id": "m2", "type": "text", "text": {"body": "/start"}},
        )
    )
    assert sent == ["welcome"]


def test_router_cmd_callback_dispatches_handler(router_env, monkeypatch):
    called: dict[str, bool] = {}

    async def fake_due(update, context):
        called["due"] = True

    monkeypatch.setitem(router_env._COMMANDS, "due", fake_due)
    _run(
        router_env.process_whatsapp_message(
            "1111111111",
            {
                "id": "m3",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": f"{CMD_PREFIX}due", "title": "⏰ Due tasks"},
                },
            },
        )
    )
    assert called.get("due")


def test_router_cmd_help_shows_help_menu(router_env, monkeypatch):
    sent: list[str] = []

    async def fake_menu(phone, *, style="menu"):
        sent.append(style)
        return True

    monkeypatch.setattr(router_env, "send_main_menu", fake_menu)
    _run(
        router_env.process_whatsapp_message(
            "1111111111",
            {
                "id": "m4",
                "type": "interactive",
                "interactive": {
                    "type": "list_reply",
                    "list_reply": {"id": f"{CMD_PREFIX}help", "title": "❓ All commands"},
                },
            },
        )
    )
    assert sent == ["help"]


def test_mark_read_includes_typing_indicator(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    from config import get_settings

    get_settings.cache_clear()

    from services.messaging import whatsapp as wa

    captured: list[dict] = []

    class FakeHTTP:
        async def post(self, url, headers=None, json=None, timeout=None):
            captured.append(json or {})

            class Resp:
                status_code = 200

                def json(self):
                    return {}

            return Resp()

    monkeypatch.setattr(wa, "_http", lambda: FakeHTTP())
    client = wa.WhatsAppClient(get_settings())
    _run(client.mark_read("wamid.1", typing=True))
    assert captured
    assert captured[0]["typing_indicator"] == {"type": "text"}
    assert captured[0]["status"] == "read"
    assert captured[0]["message_id"] == "wamid.1"


def test_mark_read_without_typing_omits_indicator(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
    from config import get_settings

    get_settings.cache_clear()

    from services.messaging import whatsapp as wa

    captured: list[dict] = []

    class FakeHTTP:
        async def post(self, url, headers=None, json=None, timeout=None):
            captured.append(json or {})

            class Resp:
                status_code = 200

                def json(self):
                    return {}

            return Resp()

    monkeypatch.setattr(wa, "_http", lambda: FakeHTTP())
    client = wa.WhatsAppClient(get_settings())
    _run(client.mark_read("wamid.2"))
    assert captured
    assert "typing_indicator" not in captured[0]
