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
