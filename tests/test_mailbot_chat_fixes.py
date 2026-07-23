"""Tests for compose/memory/date fixes from the Saleena chat diagnosis."""

from unittest.mock import patch

from ai.assistant import (
    _deterministic_date_time_answer,
    _email_from_mailbox_results,
    _is_date_time_question,
    answer_question,
    infer_person_email,
    resolve_person_email_live,
)
from bot.handlers import (
    _is_compose_recipient_reply,
    _looks_like_compose_breakout,
    _looks_like_recipient_only_message,
)
from services.conversation import get_convo, remember_turn


def test_date_time_question_detected():
    assert _is_date_time_question("what is the date today")
    assert _is_date_time_question("what day is it")
    assert _is_date_time_question("what time is it")
    assert not _is_date_time_question("what's due today")
    assert not _is_date_time_question("mails today from saleena")


def test_deterministic_date_time_answer_contains_ist():
    answer = _deterministic_date_time_answer("what is the date today")
    assert "IST" in answer
    assert "Today is" in answer


def test_answer_question_date_without_llm():
    with patch("ai.assistant.call_text", return_value=None):
        answer = answer_question("what is the date today", items=[])
    assert "IST" in answer
    assert "Today is" in answer
    assert "No open deadlines" not in answer


def test_email_from_mailbox_results_picks_sender():
    emails = [
        {
            "sender": '"Dr. Saleena Khan" <skhan@imtnag.ac.in>',
            "subject": "MBTI Questionnaire",
        },
        {
            "sender": '"Dr. Saleena Khan" <skhan@imtnag.ac.in>',
            "subject": "PROJECT THEMES",
        },
    ]
    with patch("ai.assistant.upsert_contact_from_sender") as upsert:
        email = _email_from_mailbox_results("saleena mam", emails)
    assert email == "skhan@imtnag.ac.in"
    upsert.assert_called_once()


def test_resolve_person_email_live_from_gmail():
    batch = [
        {"sender": '"Dr. Saleena Khan" <skhan@imtnag.ac.in>', "subject": "Reading"},
    ]
    with (
        patch("ai.assistant.resolve_contact_email", return_value=None),
        patch("ai.assistant.list_message_summaries", return_value=batch),
        patch("ai.assistant.upsert_contact_from_sender") as upsert,
    ):
        email = resolve_person_email_live("saleena mam")
    assert email == "skhan@imtnag.ac.in"
    upsert.assert_called_once()


def test_infer_person_email_uses_convo_memory():
    convo = {
        "last_person": "saleena mam",
        "last_person_email": "skhan@imtnag.ac.in",
    }
    with patch("ai.assistant.resolve_contact_email", return_value=None):
        email = infer_person_email("saleena", convo=convo)
    assert email == "skhan@imtnag.ac.in"


def test_compose_breakout_allows_received_mail_from_person():
    assert not _looks_like_compose_breakout("We have received mail from saleena")
    assert not _looks_like_compose_breakout("bro how can you forget we were talking about sending her a mail")


def test_compose_recipient_reply_detects_clarification():
    convo = {"last_person": "saleena mam", "last_person_email": "skhan@imtnag.ac.in"}
    compose = {"step": "need_content", "recipient": None}
    assert _is_compose_recipient_reply("We have received mail from saleena", convo, compose)
    assert _looks_like_recipient_only_message("We have received mail from saleena")


def test_remember_turn_stores_person_email():
    user_data: dict = {}
    remember_turn(
        user_data,
        user_text="mails from saleena mam",
        bot_text="8 emails from Dr. Saleena Khan",
        intent="mailbox_qa",
        person="saleena mam",
        person_email="skhan@imtnag.ac.in",
    )
    convo = get_convo(user_data)
    assert convo["last_person"] == "saleena"
    assert convo["last_person_email"] == "skhan@imtnag.ac.in"
