"""Tests for conversation memory, intent routing, and contact resolution."""

from ai.intent import (
    INTENT_COMPOSE,
    INTENT_CONTACT_LOOKUP,
    INTENT_MAILBOX_QA,
    classify_message,
)
from services.contacts import normalize_name_key, parse_sender, sender_matches_name
from services.conversation import clear_convo, get_convo, remember_turn


def test_compose_intent_send_to_email():
    result = classify_message("send a mail to sharmakavish14@gmail.com")
    assert result["intent"] == INTENT_COMPOSE
    assert "sharmakavish14@gmail.com" in (result.get("recipient") or "")


def test_retrieval_intent_send_all_from_person():
    result = classify_message("send all mails from saleena mam")
    assert result["intent"] == INTENT_MAILBOX_QA


def test_contact_lookup_intent():
    result = classify_message("what is mail id of santhi mam")
    assert result["intent"] == INTENT_CONTACT_LOOKUP
    assert result.get("person")


def test_compose_with_content():
    result = classify_message(
        "send a mail to sharmakavish14@gmail.com saying ready to ship by end of the week"
    )
    assert result["intent"] == INTENT_COMPOSE
    assert result.get("content")


def test_parse_sender_display_email():
    name, email = parse_sender('Dr. Santhi Perumal <santhi.perumal@imtnag.ac.in>')
    assert name == "Dr. Santhi Perumal"
    assert email == "santhi.perumal@imtnag.ac.in"


def test_sender_matches_requested_name():
    assert sender_matches_name(
        "Dr. Santhi Perumal <santhi.perumal@imtnag.ac.in>",
        "santhi mam",
    )
    assert not sender_matches_name(
        "LRC IMTNAG <lrc@imtnag.ac.in>",
        "santhi mam",
    )


def test_normalize_name_key_strips_honorifics():
    assert normalize_name_key("santhi mam") == "santhi"
    assert normalize_name_key("Dr. Saleena Khan") == "saleena khan"


def test_conversation_memory_remember_turn():
    user_data: dict = {}
    remember_turn(
        user_data,
        user_text="mails from saleena mam",
        bot_text="Here are 3 mails",
        intent="mailbox_qa",
        person="saleena mam",
        query="mails from saleena mam",
    )
    convo = get_convo(user_data)
    assert convo["last_person"] == "saleena mam"
    assert convo["last_query"] == "mails from saleena mam"
    assert len(convo["history"]) == 2


def test_clear_convo():
    user_data = {"convo": {"last_person": "x"}, "compose": {"step": "need_content"}}
    clear_convo(user_data)
    assert "convo" not in user_data
    assert "compose" not in user_data
