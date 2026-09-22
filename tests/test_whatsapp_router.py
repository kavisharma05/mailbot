import pytest

from bot.whatsapp_router import _COMMANDS


def test_command_map_includes_core_commands():
    for cmd in ("start", "due", "send", "ask", "status", "classes"):
        assert cmd in _COMMANDS


def test_whatsapp_actor_is_the_telegram_owner_not_the_phone(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "424242")
    from config import get_settings

    get_settings.cache_clear()
    from bot.whatsapp_adapter import build_text_update

    update = build_text_update("919999999999", "hi", "wamid.1")
    assert update.effective_user.id == 424242
    assert update.effective_chat.id == 919999999999
    get_settings.cache_clear()


def test_whatsapp_hi_reaches_chat(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "919999999999")
    from config import get_settings

    get_settings.cache_clear()
    from bot import whatsapp_router

    seen: list[str] = []

    async def fake_load(_phone):
        return {}

    async def fake_save(_phone, _data):
        return None

    async def fake_chat(update, context, **kwargs):
        seen.append(update.message.text)
        await update.message.reply_text("Hey — ask about deadlines, mail, or classes, or send /menu.")

    monkeypatch.setattr(whatsapp_router, "load_user_data", fake_load)
    monkeypatch.setattr(whatsapp_router, "save_user_data", fake_save)
    monkeypatch.setattr(whatsapp_router, "announce_pending_digest", AsyncMock(return_value=False))
    monkeypatch.setattr(whatsapp_router.handlers, "chat_message", fake_chat)
    monkeypatch.setattr(
        "bot.whatsapp_adapter.get_whatsapp_client",
        lambda: type("C", (), {"send_text": AsyncMock(return_value=True)})(),
    )

    asyncio.run(
        whatsapp_router.process_whatsapp_message(
            "919999999999",
            {"id": "m-hi", "type": "text", "text": {"body": "hi"}},
        )
    )
    assert seen == ["hi"]
    get_settings.cache_clear()


def test_whatsapp_message_has_entities_so_chat_does_not_crash():
    from bot.whatsapp_adapter import build_text_update

    update = build_text_update("919999999999", "send her a mail", "wamid.1")
    assert update.message.entities is None
    assert update.message.reply_to_message is None
    assert update.effective_chat.type == "private"


def test_whatsapp_due_during_compose_does_not_leave_draft():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from bot.whatsapp_router import _dispatch_callback, _dispatch_text

    user_data = {
        "compose": {
            "step": "confirm",
            "recipient": "a@b.com",
            "draft": {"subject": "Hi", "body": "Body"},
        }
    }

    async def _run():
        with patch("bot.whatsapp_router.get_whatsapp_client") as client:
            client.return_value.send_text = AsyncMock()
            await _dispatch_text("/due", "919999999999", "m1", user_data)
            await _dispatch_callback("cmd:due", "919999999999", user_data)
            assert client.return_value.send_text.await_count == 2
        assert user_data["compose"]["draft"]["subject"] == "Hi"

    asyncio.run(_run())


def test_whatsapp_send_menu_does_not_wipe_waiting_draft():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from bot.handlers import COMPOSE_CONFIRM, send_command

    user_data = {
        "compose": {
            "step": COMPOSE_CONFIRM,
            "recipient": "a@b.com",
            "draft": {"subject": "Hi", "body": "Body"},
        }
    }
    update = MagicMock()
    update.message.text = "/send"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = user_data
    context.args = []

    async def _run():
        with patch("bot.handlers._is_owner", return_value=True):
            await send_command(update, context)
        assert user_data["compose"]["draft"]["subject"] == "Hi"
        assert "waiting" in update.message.reply_text.call_args.args[0].lower()

    asyncio.run(_run())


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
