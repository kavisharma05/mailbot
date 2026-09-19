"""Fixes from the 19 Sep WhatsApp chat: stale classes day, Ashish mail, Claude."""

from datetime import date
from unittest.mock import patch

from ai.assistant import _build_mailbox_queries, _extract_person_query, _format_mailbox_fallback
from ai.intent import INTENT_MAILBOX_QA, classify_message
from services.contacts import clean_person_phrase, extract_person_reference
from services.schedule import is_schedule_challenge, resolve_schedule_day
from services.timeutil import today_ist


def test_bare_classes_does_not_reuse_stale_day():
    day = resolve_schedule_day("Classes", fallback_iso="2026-09-16")
    assert day == today_ist()


def test_schedule_challenge_snaps_to_today_not_mentioned_date():
    text = "Why are you showing classes for date 16th sep"
    assert is_schedule_challenge(text)
    assert resolve_schedule_day(text, fallback_iso="2026-09-16") == today_ist()


def test_those_classes_can_reuse_last_day():
    assert resolve_schedule_day("those classes", fallback_iso="2026-09-16") == date(2026, 9, 16)


def test_extract_person_from_check_ashish_mail():
    assert _extract_person_query("Check ashish mail") == "ashish"
    assert clean_person_phrase("Check ashish mail") == "ashish"
    assert extract_person_reference("Check ashish mail") == "ashish"


def test_check_ashish_mail_is_mailbox_with_person():
    result = classify_message("Check ashish mail")
    assert result["intent"] == INTENT_MAILBOX_QA
    assert (result.get("person") or "").lower() == "ashish"


def test_ashish_mailbox_query_uses_from_not_raw_sentence():
    queries = _build_mailbox_queries("Check ashish mail")
    assert queries
    assert all("Check ashish" not in q for q in queries)
    assert any("from:" in q.lower() for q in queries)


def test_mailbox_fallback_count_uses_listed_emails():
    text = _format_mailbox_fallback(
        {"query": "from:ashish newer_than:90d", "count": 0, "listed": 2},
        [
            {"subject": "Hi", "sender": "Ashish Kumar <2026204@imtnag.ac.in>", "received_at": "Tue"},
            {"subject": "Re", "sender": "Ashish Kumar <2026204@imtnag.ac.in>", "received_at": "Mon"},
        ],
        "Check ashish mail",
    )
    assert "Matching mails: 0" not in text
    assert "Ashish Kumar" in text
