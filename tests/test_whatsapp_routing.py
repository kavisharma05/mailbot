import pytest


def test_owner_phone_resolves_without_map(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "917489491188")
    monkeypatch.setenv("WHATSAPP_PHONE_MAP", "")
    from config import get_settings
    from services.messaging.whatsapp_routing import (
        clear_whatsapp_routing_cache,
        resolve_whatsapp_bot_user,
        whatsapp_phone_allowed,
    )

    get_settings.cache_clear()
    clear_whatsapp_routing_cache()

    monkeypatch.setattr(
        "services.messaging.whatsapp_routing.get_owner_user",
        lambda: {"id": "owner-1", "role": "owner", "telegram_user_id": "424242"},
    )
    assert whatsapp_phone_allowed("917489491188")
    assert resolve_whatsapp_bot_user("917489491188")["role"] == "owner"
    assert not whatsapp_phone_allowed("917984586096")
    get_settings.cache_clear()
    clear_whatsapp_routing_cache()


def test_mapped_peer_phone(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "917489491188")
    monkeypatch.setenv("WHATSAPP_PHONE_MAP", "917984586096:tripti,919142052201:ashish")
    from config import get_settings
    from services.messaging.whatsapp_routing import (
        clear_whatsapp_routing_cache,
        resolve_whatsapp_bot_user,
        whatsapp_phone_allowed,
    )

    get_settings.cache_clear()
    clear_whatsapp_routing_cache()

    def fake_resolve(key):
        if key == "tripti":
            return {"id": "u-t", "role": "student", "telegram_user_id": "111", "display_name": "Tripti"}
        if key == "ashish":
            return {"id": "u-a", "role": "student", "telegram_user_id": "222", "display_name": "Ashish"}
        return None

    monkeypatch.setattr("services.messaging.whatsapp_routing._resolve_map_key", fake_resolve)

    assert whatsapp_phone_allowed("917984586096")
    assert resolve_whatsapp_bot_user("917984586096")["id"] == "u-t"
    assert resolve_whatsapp_bot_user("919142052201")["id"] == "u-a"
    assert not whatsapp_phone_allowed("919999999999")
    get_settings.cache_clear()
    clear_whatsapp_routing_cache()


def test_whatsapp_actor_uses_peer_telegram_id(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_PHONE", "917489491188")
    monkeypatch.setenv("TELEGRAM_OWNER_USER_ID", "424242")
    monkeypatch.setenv("WHATSAPP_PHONE_MAP", "917984586096:tripti")
    from config import get_settings
    from services.messaging.whatsapp_routing import clear_whatsapp_routing_cache

    get_settings.cache_clear()
    clear_whatsapp_routing_cache()

    monkeypatch.setattr(
        "services.messaging.whatsapp_routing.resolve_whatsapp_bot_user",
        lambda _p: {"id": "u-t", "telegram_user_id": "555001"},
    )
    from bot.whatsapp_adapter import build_text_update

    update = build_text_update("917984586096", "hi", "wamid.1")
    assert update.effective_user.id == 555001
    assert update.whatsapp_phone == "917984586096"
    get_settings.cache_clear()
    clear_whatsapp_routing_cache()
