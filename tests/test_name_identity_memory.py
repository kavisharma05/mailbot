"""Tests for canonical person identity and conversation memory window."""

import asyncio
import json
from unittest.mock import patch

from ai.assistant import _build_user_prompt
from ai.intent import _extract_person_after_preposition
from services.contacts import canonical_person_name, resolve_contact
from services.conversation import MAX_HISTORY, get_convo, remember_turn
from services.persistence import SupabasePersistence


def test_canonical_person_name_strips_honorifics():
    assert canonical_person_name("saleena mam") == "saleena"
    assert canonical_person_name("Dr. Saleena Khan") == "saleena khan"
    assert canonical_person_name("saleena khan") == "saleena khan"


def test_intent_extract_person_strips_honorifics():
    assert _extract_person_after_preposition("mails from saleena mam", "from") == "saleena"
    assert _extract_person_after_preposition("mails from saleena khan", "from") == "saleena khan"


def test_remember_turn_canonicalizes_last_person():
    user_data: dict = {}
    remember_turn(user_data, user_text="hi", person="saleena mam")
    assert get_convo(user_data)["last_person"] == "saleena"


def test_resolve_contact_searches_all_tokens():
    rows = [
        {
            "email": "skhan@imtnag.ac.in",
            "display_name": "Dr. Saleena Khan",
            "name_key": "saleena khan",
            "message_count": 8,
        }
    ]

    def fake_find(token: str, limit: int = 20):
        if token in {"saleena", "khan"}:
            return rows
        return []

    with patch("services.contacts.find_contacts_by_name", side_effect=fake_find):
        by_mam = resolve_contact("saleena mam")
        by_khan = resolve_contact("saleena khan")
        by_surname = resolve_contact("khan")

    assert len(by_mam) == 1
    assert by_mam[0]["email"] == "skhan@imtnag.ac.in"
    assert by_khan[0]["email"] == "skhan@imtnag.ac.in"
    assert by_surname[0]["email"] == "skhan@imtnag.ac.in"


def test_max_history_is_twelve():
    assert MAX_HISTORY == 12


def test_history_window_keeps_twelve_turns():
    user_data: dict = {}
    for i in range(15):
        remember_turn(user_data, user_text=f"user-{i}", bot_text=f"bot-{i}")
    history = get_convo(user_data)["history"]
    assert len(history) == MAX_HISTORY * 2
    assert history[0]["text"] == "user-3"
    assert history[-1]["text"] == "bot-14"


def test_build_user_prompt_includes_recent_history():
    convo = {
        "last_person": "saleena khan",
        "last_person_email": "skhan@imtnag.ac.in",
        "last_query": "mails from saleena",
        "last_intent": "mailbox_qa",
        "history": [
            {"role": "user", "text": "mails from saleena khan"},
            {"role": "bot", "text": "8 emails found"},
        ],
    }
    prompt = _build_user_prompt("send her a mail", [], [], None, None, convo=convo)
    assert "recent_history" in prompt
    assert "mails from saleena khan" in prompt
    assert "8 emails found" in prompt


def test_supabase_persistence_round_trip():
    saved: dict[str, dict] = {}

    def fake_load():
        return dict(saved)

    def fake_save(user_id: str, data: dict):
        saved[user_id] = data

    with (
        patch("services.persistence.load_all_user_data_states", side_effect=fake_load),
        patch("services.persistence.save_user_data_state", side_effect=fake_save),
    ):
        persistence = SupabasePersistence(update_interval=999)
        user_data = {"convo": {"last_person": "saleena khan", "history": []}}

        async def _run():
            await persistence.update_user_data(123, user_data)
            reloaded = SupabasePersistence(update_interval=999)
            restored = await reloaded.get_user_data()
            assert restored[123]["convo"]["last_person"] == "saleena khan"

        asyncio.run(_run())


def test_load_save_user_data_state_fallback():
    from services import db as db_module

    db_module._user_state_table_missing = True
    db_module._user_state_fallback.clear()
    try:
        db_module.save_user_data_state("42", {"convo": {"last_person": "saleena"}})
        loaded = db_module.load_all_user_data_states()
        assert loaded["42"]["convo"]["last_person"] == "saleena"
    finally:
        db_module._user_state_table_missing = False
        db_module._user_state_fallback.clear()
