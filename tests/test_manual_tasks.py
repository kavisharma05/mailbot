"""Tests for undated list, manual add, and manual-only edit."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from ai.assistant import _asks_undated_tasks, _is_add_task_question
from ai.intent import INTENT_ADD_TASK, INTENT_UNDATED_LIST, classify_message
from bot.handlers import _parse_add_one_line
from services.db import (
    create_manual_assignment,
    get_undated_assignments,
    update_assignment,
)
from services.formatting import format_undated_list, sort_undated_assignments
from services.timeutil import IST, to_storage_iso


def _iso(dt: datetime) -> str:
    return to_storage_iso(dt)


def test_get_undated_assignments_filters_resolvable_deadlines(monkeypatch):
    now = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    items = [
        {
            "id": "undated",
            "category": "other",
            "task": "Read later",
            "deadline_date": None,
            "deadline_text": "soon",
            "created_at": _iso(now - timedelta(days=1)),
        },
        {
            "id": "dated",
            "category": "deadline",
            "task": "Submit",
            "deadline_date": _iso(now + timedelta(days=1)),
            "deadline_text": None,
            "created_at": _iso(now),
        },
    ]
    monkeypatch.setattr("services.db.get_open_assignments", lambda **kwargs: items)
    result = get_undated_assignments(now=now)
    assert [i["id"] for i in result] == ["undated"]


def test_create_manual_assignment_sets_source_and_deadline():
    mock_table = MagicMock()
    mock_execute = MagicMock()
    mock_execute.data = [{"id": "new-1", "source": "manual", "task": "Submit CV"}]
    mock_table.insert.return_value.execute.return_value = mock_execute

    with patch("services.db.get_db") as mock_get_db:
        mock_get_db.return_value.table.return_value = mock_table
        row = create_manual_assignment(
            "Submit CV",
            deadline_text="tomorrow 5pm",
            category="placement",
        )

    assert row is not None
    payload = mock_table.insert.call_args[0][0]
    assert payload["source"] == "manual"
    assert payload["task"] == "Submit CV"
    assert payload["category"] == "placement"
    assert payload["deadline_text"] == "tomorrow 5pm"
    assert payload["deadline_date"] is not None


def test_create_manual_assignment_allows_no_deadline():
    mock_table = MagicMock()
    mock_execute = MagicMock()
    mock_execute.data = [{"id": "new-2", "source": "manual"}]
    mock_table.insert.return_value.execute.return_value = mock_execute

    with patch("services.db.get_db") as mock_get_db:
        mock_get_db.return_value.table.return_value = mock_table
        create_manual_assignment("Read chapter 3", category="other")

    payload = mock_table.insert.call_args[0][0]
    assert payload["deadline_date"] is None
    assert payload["deadline_text"] is None


def test_update_assignment_manual_only():
    chain = MagicMock()
    chain.eq.return_value = chain
    chain.execute.return_value.data = [{"id": "m1", "source": "manual", "task": "Updated"}]

    mock_table = MagicMock()
    mock_table.update.return_value = chain

    with patch("services.db.get_db") as mock_get_db:
        mock_get_db.return_value.table.return_value = mock_table
        row = update_assignment("m1", {"task": "Updated"})

    assert row is not None
    mock_table.update.assert_called_once()
    chain.eq.assert_any_call("id", "m1")
    chain.eq.assert_any_call("source", "manual")
    chain.eq.assert_any_call("status", "open")


def test_update_assignment_recomputes_deadline_date():
    chain = MagicMock()
    chain.eq.return_value = chain
    chain.execute.return_value.data = [{"id": "m1", "source": "manual"}]

    mock_table = MagicMock()
    mock_table.update.return_value = chain

    with patch("services.db.get_db") as mock_get_db:
        mock_get_db.return_value.table.return_value = mock_table
        update_assignment("m1", {"deadline_text": "tomorrow 3pm"})

    payload = mock_table.update.call_args[0][0]
    assert payload["deadline_text"] == "tomorrow 3pm"
    assert payload["deadline_date"] is not None


def test_format_undated_list_empty_state():
    text, ids = format_undated_list([], None)
    assert ids == []
    assert "Open tasks (no due date)" in text
    assert "No open tasks without" in text


def test_sort_undated_newest_first():
    older = {"id": "a", "created_at": "2026-07-20T10:00:00+05:30", "task": "Old"}
    newer = {"id": "b", "created_at": "2026-07-23T10:00:00+05:30", "task": "New"}
    ordered = sort_undated_assignments([older, newer])
    assert [i["id"] for i in ordered] == ["b", "a"]


def test_parse_add_one_line():
    parsed = _parse_add_one_line("Submit report | tomorrow 5pm | deadline")
    assert parsed["task"] == "Submit report"
    assert parsed["deadline_text"] == "tomorrow 5pm"
    assert parsed["category"] == "deadline"


def test_parse_add_one_line_task_only():
    parsed = _parse_add_one_line("Just a task")
    assert parsed["task"] == "Just a task"
    assert parsed["deadline_text"] is None
    assert parsed["category"] == "other"


@pytest.mark.parametrize(
    "question",
    ["tasks with no deadline", "show undated tasks", "open tasks without a due date"],
)
def test_undated_intent_detection(question: str):
    assert _asks_undated_tasks(question) is True


def test_classify_message_routes_undated_intent():
    result = classify_message("show undated tasks", None)
    assert result["intent"] == INTENT_UNDATED_LIST


@pytest.mark.parametrize(
    "question",
    ["add a task submit report", "remind me to call placement office", "new task buy books"],
)
def test_add_task_intent_detection(question: str):
    assert _is_add_task_question(question) is True


def test_classify_message_routes_add_task_intent():
    result = classify_message("add a task submit report", None)
    assert result["intent"] == INTENT_ADD_TASK
