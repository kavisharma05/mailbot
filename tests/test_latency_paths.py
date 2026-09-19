"""Latency shortcuts: skip Gmail/LLM when local data is enough."""

from unittest.mock import patch

from ai.assistant import _gather_context_for_question, infer_person_email
from app import fastapi_app
from fastapi.testclient import TestClient
from services.web_conversation import clear_web_convo

client = TestClient(fastapi_app)


def test_gather_context_skips_gmail_for_due_list():
    with (
        patch("ai.assistant._gather_mailbox_context") as mailbox,
        patch("ai.assistant._fetch_relevant_emails") as relevant,
        patch("ai.assistant.count_messages") as count,
    ):
        stats, emails = _gather_context_for_question(
            "what's due today",
            scan_gmail=True,
            pasted_email=None,
        )
    assert stats is None
    assert emails == []
    mailbox.assert_not_called()
    relevant.assert_not_called()
    count.assert_not_called()


def test_gather_context_skips_gmail_for_generic_questions():
    with (
        patch("ai.assistant._gather_mailbox_context") as mailbox,
        patch("ai.assistant._fetch_relevant_emails") as relevant,
    ):
        stats, emails = _gather_context_for_question(
            "help me plan robotics",
            scan_gmail=True,
            pasted_email=None,
        )
    assert stats is None
    assert emails == []
    mailbox.assert_not_called()
    relevant.assert_not_called()


def test_infer_person_email_can_skip_live_gmail():
    with patch("ai.assistant.resolve_contact_email", return_value=None):
        with patch("ai.assistant.resolve_person_email_live") as live:
            email = infer_person_email("saleena", convo={}, live=False)
    assert email is None
    live.assert_not_called()


def test_web_chat_due_list_skips_llm():
    clear_web_convo()
    with (
        patch("api.web_routes.answer_question") as mock_qa,
        patch("api.web_routes.get_assignments_due_on", return_value=[]),
        patch("api.web_routes.get_assignments_due_between", return_value=[]),
        patch("api.web_routes.get_upcoming_assignments", return_value=[]),
    ):
        r = client.post("/api/chat/message", json={"message": "what's due today"})
    mock_qa.assert_not_called()
    assert r.status_code == 200
    assert r.json()["reply"]


def test_web_chat_schedule_challenge_uses_today():
    from services.timeutil import today_ist

    clear_web_convo()
    with (
        patch("api.web_routes.answer_question") as mock_qa,
        patch("api.web_routes.get_preferred_section", return_value="C"),
        patch("api.web_routes.sessions_for_day", return_value=[]) as mock_day,
        patch("api.web_routes.next_sessions_after", return_value=(None, [])),
    ):
        r = client.post(
            "/api/chat/message",
            json={"message": "Why are you showing classes for date 16th sep"},
        )
    mock_qa.assert_not_called()
    assert r.status_code == 200
    assert mock_day.call_args.args[0] == today_ist()


def test_web_chat_schedule_skips_llm():
    clear_web_convo()
    with (
        patch("api.web_routes.answer_question") as mock_qa,
        patch("api.web_routes.get_preferred_section", return_value="A"),
        patch(
            "api.web_routes.sessions_for_day",
            return_value=[
                {
                    "course_or_topic": "Robotics",
                    "start_time": "09:00",
                    "end_time": "10:30",
                    "room": "C-12",
                }
            ],
        ),
    ):
        r = client.post("/api/chat/message", json={"message": "Today's schedule"})
    mock_qa.assert_not_called()
    assert r.status_code == 200
    assert "Robotics" in r.json()["reply"]
