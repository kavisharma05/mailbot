"""Regression tests for bug hunt round 2 fixes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ai.client as client_module
from ai.assistant import _looks_like_pasted_email, split_question_and_paste
from ai.client import _call_groq, _cooldowns
from groq import APIStatusError
from services.schedule import format_schedule_share


def test_looks_like_pasted_email_short_question():
    assert _looks_like_pasted_email("what's due tomorrow?") is False


def test_looks_like_pasted_email_long_dear_regards():
    blob = "Dear students, " + ("please complete the assignment. " * 20) + " Regards, Prof"
    assert _looks_like_pasted_email(blob) is True


def test_split_question_and_paste_short_question():
    question, pasted = split_question_and_paste("what's due tomorrow?")
    assert question == "what's due tomorrow?"
    assert pasted is None


def test_split_question_and_paste_splits_long_email():
    intro = "What does this mean?"
    body = "Dear students,\n" + ("Please read chapter 3. " * 30) + "\nRegards,\nProfessor"
    question, pasted = split_question_and_paste(intro + "\n" + body)
    assert "What does this mean?" in question
    assert pasted is not None
    assert "Regards" in pasted
    assert "Please read chapter 3" in pasted


def test_groq_auth_error_applies_cooldown():
    client_module._cooldowns.clear()
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = APIStatusError(
        "Invalid API Key",
        response=MagicMock(status_code=401),
        body=None,
    )

    with patch("ai.client.Groq", return_value=mock_client):
        result = _call_groq(
            "system",
            "user",
            json_mode=False,
            max_tokens=100,
            temperature=0.3,
            api_key="bad-key",
            model="test-model",
            provider="groq",
        )

    assert result is None
    assert "groq" in client_module._cooldowns
    client_module._cooldowns.clear()


def test_format_schedule_share_missing_session_date():
    sessions = [
        {
            "course_or_topic": "OB",
            "start_time": "09:45",
            "end_time": "11:15",
            "room": "AEC Hall",
        },
        {
            "session_date": "2026-07-24",
            "course_or_topic": "Marketing",
            "start_time": "14:00",
            "end_time": "15:30",
            "room": "Arthum#1",
        },
    ]
    text = format_schedule_share(sessions)
    assert "Class schedule received" in text
    assert "OB" in text
    assert "Marketing" in text
