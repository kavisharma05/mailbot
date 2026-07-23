"""Tests for upcoming vs missed deadline filtering and list formatting."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from ai.assistant import _deterministic_deadline_answer, _is_due_list_question, _is_missed_list_question
from ai.intent import INTENT_DUE_LIST, INTENT_MISSED_LIST, classify_message
from services.db import (
    _filter_assignments_by_deadline,
    get_assignments_due_on,
    get_missed_assignments,
    get_upcoming_assignments,
)
from services.formatting import (
    format_dated_due_list,
    format_due_list,
    format_missed_list,
    sort_missed_assignments,
    sort_upcoming_assignments,
)
from services.timeutil import IST, normalize_date_words, parse_day_window, to_storage_iso


def _iso(dt: datetime) -> str:
    return to_storage_iso(dt)


def _item(item_id: str, deadline: datetime, *, category: str = "deadline") -> dict:
    return {
        "id": item_id,
        "category": category,
        "task": f"Task {item_id}",
        "deadline_date": _iso(deadline),
        "deadline_text": None,
        "course_or_topic": "Course",
        "created_at": _iso(deadline - timedelta(days=2)),
    }


def test_filter_splits_upcoming_and_missed_at_boundary():
    now = datetime(2026, 7, 23, 15, 0, tzinfo=IST)
    past = now - timedelta(minutes=1)
    future = now + timedelta(minutes=1)
    items = [_item("past", past), _item("future", future), {"id": "none", "category": "deadline"}]

    upcoming = _filter_assignments_by_deadline(items, upcoming=True, now=now)
    missed = _filter_assignments_by_deadline(items, upcoming=False, now=now)

    assert [i["id"] for i in upcoming] == ["future"]
    assert [i["id"] for i in missed] == ["past"]


def test_filter_includes_exact_now_in_upcoming():
    now = datetime(2026, 7, 23, 12, 0, tzinfo=IST)
    items = [_item("now", now)]

    upcoming = _filter_assignments_by_deadline(items, upcoming=True, now=now)
    missed = _filter_assignments_by_deadline(items, upcoming=False, now=now)

    assert [i["id"] for i in upcoming] == ["now"]
    assert missed == []


def test_get_upcoming_and_missed_use_open_assignments(monkeypatch):
    now = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    past = now - timedelta(hours=2)
    future = now + timedelta(hours=2)
    open_items = [_item("past", past), _item("future", future)]

    monkeypatch.setattr("services.db.get_open_assignments", lambda **kwargs: open_items)

    assert [i["id"] for i in get_upcoming_assignments(now=now)] == ["future"]
    assert [i["id"] for i in get_missed_assignments(now=now)] == ["past"]


def test_sort_upcoming_nearest_deadline_first():
    base = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    items = [
        _item("far", base + timedelta(days=3), category="placement"),
        _item("near", base + timedelta(hours=1), category="other"),
    ]
    ordered = sort_upcoming_assignments(items)
    assert [i["id"] for i in ordered] == ["near", "far"]


def test_sort_missed_most_recent_first():
    base = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    items = [
        _item("old", base - timedelta(days=3)),
        _item("recent", base - timedelta(hours=1)),
    ]
    ordered = sort_missed_assignments(items)
    assert [i["id"] for i in ordered] == ["recent", "old"]


def test_format_due_list_empty_state():
    text, ids = format_due_list([], None)
    assert ids == []
    assert "Upcoming deadlines" in text
    assert "No upcoming deadlines" in text


def test_format_missed_list_empty_state():
    text, ids = format_missed_list([], "deadline")
    assert ids == []
    assert "Missed deadlines" in text
    assert "Nothing missed" in text


def test_format_due_list_numbering_and_truncation():
    base = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    items = [_item(str(i), base + timedelta(hours=i)) for i in range(25)]
    text, ids = format_due_list(items, None)
    assert len(ids) == 20
    assert "1." in text
    assert "…and 5 more." in text


def test_format_missed_list_marks_overdue_rows():
    base = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    items = [_item("late", base - timedelta(hours=2))]
    text, ids = format_missed_list(items, None)
    assert ids == ["late"]
    assert "Overdue" in text


def test_text_only_deadline_resolved_from_created_at():
    created = datetime(2026, 7, 20, 9, 0, tzinfo=IST)
    now = datetime(2026, 7, 23, 10, 0, tzinfo=IST)
    item = {
        "id": "text-only",
        "category": "deadline",
        "task": "Submit report",
        "deadline_date": None,
        "deadline_text": "tomorrow 5pm",
        "created_at": _iso(created),
        "course_or_topic": "Course",
    }
    upcoming = _filter_assignments_by_deadline([item], upcoming=True, now=now)
    missed = _filter_assignments_by_deadline([item], upcoming=False, now=now)
    assert upcoming == []
    assert [i["id"] for i in missed] == ["text-only"]


@pytest.mark.parametrize(
    "question",
    [
        "what deadlines did I miss?",
        "show overdue deadlines",
        "missed deadlines",
    ],
)
def test_missed_list_question_detection(question: str):
    assert _is_missed_list_question(question) is True


def test_missed_list_question_not_triggered_for_mail_lookup():
    assert _is_missed_list_question("missed emails from prof") is False


def test_classify_message_routes_missed_intent():
    result = classify_message("what deadlines did I miss?", None)
    assert result["intent"] == INTENT_MISSED_LIST


REF = datetime(2026, 7, 23, 15, 0, tzinfo=IST)


def test_normalize_date_words_fixes_typos():
    assert "tomorrow" in normalize_date_words("deadlines tmrw")
    assert "today" in normalize_date_words("due tdy")
    assert "yesterday" in normalize_date_words("anything yest")


@pytest.mark.parametrize(
    "question,expected_start,expected_end,single",
    [
        ("deadlines for tomorrow", date(2026, 7, 24), date(2026, 7, 24), True),
        ("what's due today", date(2026, 7, 23), date(2026, 7, 23), True),
        ("deadlines yesterday", date(2026, 7, 22), date(2026, 7, 22), True),
        ("deadlines tmrw", date(2026, 7, 24), date(2026, 7, 24), True),
        ("today and tomorrow deadlines", date(2026, 7, 23), date(2026, 7, 24), False),
        ("deadlines this week", date(2026, 7, 23), date(2026, 7, 26), False),
        ("deadlines for 24 Jul", date(2026, 7, 24), date(2026, 7, 24), True),
        ("do we have some deadlines for tomorrow", date(2026, 7, 24), date(2026, 7, 24), True),
    ],
)
def test_parse_day_window(question, expected_start, expected_end, single):
    window = parse_day_window(question, ref=REF)
    assert window is not None
    assert window.start == expected_start
    assert window.end == expected_end
    assert window.single is single


def test_parse_day_window_none_without_date_signal():
    assert parse_day_window("show all deadlines", ref=REF) is None
    assert parse_day_window("refresh deadline list", ref=REF) is None


def test_is_due_list_question_accepts_typos():
    assert _is_due_list_question("any deadline tmrw") is True
    assert _is_due_list_question("deadlines tdy") is True


def test_classify_message_routes_dated_due_intent():
    result = classify_message("do we have some deadlines for tomorrow", None)
    assert result["intent"] == INTENT_DUE_LIST


def test_get_assignments_due_on_filters_by_calendar_day(monkeypatch):
    today = REF.date()
    tomorrow = today + timedelta(days=1)
    open_items = [
        _item("today", datetime.combine(today, datetime.min.time(), tzinfo=IST)),
        _item("tomorrow", datetime.combine(tomorrow, datetime.min.time(), tzinfo=IST)),
    ]
    monkeypatch.setattr("services.db.get_open_assignments", lambda **kwargs: open_items)

    tomorrow_items = get_assignments_due_on(tomorrow, IST)
    assert [i["id"] for i in tomorrow_items] == ["tomorrow"]


def test_format_dated_due_list_empty_state():
    text, ids = format_dated_due_list([], "tomorrow (IST)", None)
    assert ids == []
    assert "Deadlines — tomorrow (IST)" in text
    assert "No deadlines due tomorrow (IST)." in text


def test_format_dated_due_list_shows_only_matching_items():
    tomorrow = REF.date() + timedelta(days=1)
    items = [_item("tomorrow-task", datetime.combine(tomorrow, datetime.min.time(), tzinfo=IST))]
    text, ids = format_dated_due_list(items, "tomorrow (IST)", None)
    assert ids == ["tomorrow-task"]
    assert "tomorrow-task" in text.lower() or "Task tomorrow-task" in text


def test_deterministic_deadline_answer_filters_tomorrow_only():
    today = REF.date()
    tomorrow = today + timedelta(days=1)
    items = [
        {**_item("today-task", datetime.combine(today, datetime.min.time(), tzinfo=IST)), "status": "open"},
        {**_item("tomorrow-task", datetime.combine(tomorrow, datetime.min.time(), tzinfo=IST)), "status": "open"},
    ]
    answer = _deterministic_deadline_answer("deadlines for tomorrow", items)
    assert answer is not None
    assert "tomorrow-task" in answer or "Task tomorrow-task" in answer
    assert "today-task" not in answer
