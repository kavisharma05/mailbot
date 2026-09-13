import pytest

from bot.whatsapp_router import _COMMANDS


def test_command_map_includes_core_commands():
    for cmd in ("start", "due", "send", "ask", "status", "classes"):
        assert cmd in _COMMANDS


def test_process_whatsapp_ignores_non_owner(monkeypatch):
    import asyncio

    from bot import whatsapp_router

    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "1111111111")
    from config import get_settings

    get_settings.cache_clear()

    called: dict[str, bool] = {}

    async def fake_load(_phone):
        return {}

    async def fake_save(_phone, _data):
        called["saved"] = True

    async def fake_dispatch(*_a, **_k):
        called["dispatch"] = True

    monkeypatch.setattr(whatsapp_router, "load_user_data", fake_load)
    monkeypatch.setattr(whatsapp_router, "save_user_data", fake_save)
    monkeypatch.setattr(whatsapp_router, "_dispatch_text", fake_dispatch)

    asyncio.run(
        whatsapp_router.process_whatsapp_message(
            "9999999999",
            {"id": "m1", "type": "text", "text": {"body": "/due"}},
        )
    )
    assert "dispatch" not in called
