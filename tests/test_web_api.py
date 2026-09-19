"""Smoke tests for mobile web REST API."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import fastapi_app

client = TestClient(fastapi_app)


@pytest.fixture
def mock_db_reads():
    with patch("api.web_routes.get_open_assignments", return_value=[]):
        with patch("api.web_routes.get_upcoming_assignments", return_value=[]):
            with patch("api.web_routes.get_missed_assignments", return_value=[]):
                with patch("api.web_routes.get_undated_assignments", return_value=[]):
                    with patch("api.web_routes.get_preferred_section", return_value="A"):
                        with patch("api.web_routes.sessions_for_day", return_value=[]):
                            with patch("api.web_routes.get_watch_state", return_value={}):
                                with patch("api.web_routes._activity_items", return_value=[]):
                                    yield


def test_quick_stats(mock_db_reads):
    r = client.get("/api/stats/quick")
    assert r.status_code == 200
    data = r.json()
    assert "deadlines_due" in data
    assert "classes_today" in data


def test_list_tasks(mock_db_reads):
    with patch("api.web_routes.get_db") as mock_get_db:
        mock_get_db.return_value.table.return_value.select.return_value.eq.return_value.order.return_value.limit.return_value.execute.return_value = type(
            "R", (), {"data": []}
        )()
        r = client.get("/api/tasks")
    assert r.status_code == 200
    assert "tasks" in r.json()


def test_chat_empty_message():
    r = client.post("/api/chat/message", json={"message": "   "})
    assert r.status_code == 400


def test_web_chat_asks_who_for_send_a_mail():
    from services.web_conversation import clear_web_convo

    clear_web_convo()
    with patch("api.web_routes.answer_question") as mock_qa:
        r = client.post("/api/chat/message", json={"message": "send a mail"})
    mock_qa.assert_not_called()
    assert r.status_code == 200
    assert "who" in r.json()["reply"].lower()
    assert r.json().get("draft") is None


def test_web_chat_resolves_named_recipient():
    from services.web_conversation import clear_web_convo

    clear_web_convo()
    with patch("api.web_routes.resolve_email_robust", return_value=("skhan@imtnag.ac.in", [], None)):
        r = client.post("/api/chat/message", json={"message": "send saleena a mail"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("draft") is None
    assert "skhan@imtnag.ac.in" in data["reply"]
    assert "what should the email say" in data["reply"].lower()


def test_web_chat_pronoun_after_qa_uses_last_person():
    from services.web_conversation import clear_web_convo, remember_web_turn

    clear_web_convo()
    remember_web_turn(
        user_text="mails from saleena",
        bot_text="3 mails",
        intent="qa",
        person="saleena",
        person_email="skhan@imtnag.ac.in",
    )
    with patch("api.web_routes.resolve_email_robust", return_value=("skhan@imtnag.ac.in", [], None)):
        r = client.post("/api/chat/message", json={"message": "send her a mail"})
    assert r.status_code == 200
    data = r.json()
    assert "skhan@imtnag.ac.in" in data["reply"]
    assert "what should the email say" in data["reply"].lower()


def test_web_chat_send_it_to_named_person():
    from services.web_conversation import clear_web_convo

    clear_web_convo()
    with patch("api.web_routes.resolve_email_robust", return_value=("vinay@imtnag.ac.in", [], None)):
        r = client.post("/api/chat/message", json={"message": "send it to vinay"})
    assert r.status_code == 200
    assert "vinay@imtnag.ac.in" in r.json()["reply"]


def test_short_task_detail_and_presentation_recategorize():
    from api.web_routes import _serialize_task

    task = _serialize_task(
        {
            "id": "1",
            "task": "Complete registration",
            "summary": "Complete HUL LIME registration for the current term",
            "category": "placement",
            "sender": "Placement Cell",
            "course_or_topic": "HUL LIME",
            "status": "open",
            "deadline_date": "2026-09-16T23:59:00+05:30",
        }
    )
    assert "HUL LIME" in task["task"]
    assert task["detail"] == "" or "HUL LIME" in (task["detail"] or "")

    presentation = _serialize_task(
        {
            "id": "2",
            "task": "Deliver OB group presentation",
            "summary": "10–12 min presentation covering concepts, analysis and teamwork",
            "category": "certification",
            "status": "open",
        }
    )
    assert presentation["category"] == "deadline"
    assert presentation["detail"]
