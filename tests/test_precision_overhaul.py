"""Tests for precision timing, formatting, schedule bugs, and categorization."""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from ai.deadline import _boost_priority_from_signals, normalize_classification
from services.formatting import CATEGORY_ICONS, category_icon, format_deadline
from services.reminders import PRIORITY_TIERS, _tiers_for_assignment, schedule_deadline_reminders
from services.schedule import (
    _next_day_with_classes,
    _schedule_queries,
    classes_command_text,
    is_trusted_schedule_sender,
)
from services.timeutil import IST, format_deadline_display


def test_next_day_with_classes():
    sessions = [
        {"session_date": "2026-07-24", "course_or_topic": "OB", "start_time": "09:45"},
        {"session_date": "2026-07-26", "course_or_topic": "Fin", "start_time": "11:30"},
    ]
    today = date(2026, 7, 23)
    next_day, next_sessions = _next_day_with_classes(sessions, today)
    assert next_day == date(2026, 7, 24)
    assert len(next_sessions) == 1


def test_classes_skips_gmail_when_cache_fresh():
    import time

    import services.schedule as sch

    day = date(2026, 7, 23)
    template = [
        {"session_date": "2026-07-24", "course_or_topic": "OB", "start_time": "09:45", "room": "A1"},
    ]
    sch._last_gmail_refresh_mono = time.monotonic()
    try:
        with (
            patch("services.schedule.today_ist", return_value=day),
            patch("services.schedule.refresh_schedule_from_gmail") as mock_refresh,
            patch("services.schedule.get_preferred_section", return_value="A"),
            patch("services.schedule.load_template_sessions", return_value=template),
            patch("services.schedule.sessions_for_day", return_value=[]),
        ):
            text = classes_command_text(refresh=True, section="A")
        mock_refresh.assert_not_called()
        assert "No classes today" in text
    finally:
        sch._last_gmail_refresh_mono = 0.0


def test_classes_command_text_no_classes_today_label():
    day = date(2026, 7, 23)
    template = [
        {"session_date": "2026-07-24", "course_or_topic": "OB", "start_time": "09:45", "room": "A1"},
    ]

    with patch("services.schedule.today_ist", return_value=day), patch(
        "services.schedule.refresh_schedule_from_gmail",
        return_value={"ok": True, "sessions": template, "subject": "TT"},
    ), patch("services.schedule.get_preferred_section", return_value="A"), patch(
        "services.schedule.load_template_sessions", return_value=template
    ), patch(
        "services.schedule.sessions_for_day", return_value=[]
    ):
        text = classes_command_text(refresh=True, section="A")

    assert "No classes today" in text
    assert "Next classes" in text
    assert "24 Jul" in text


def test_trusted_schedule_sender_when_configured():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = "registrar@college.edu"
        assert is_trusted_schedule_sender("Registrar <registrar@college.edu>")
        assert not is_trusted_schedule_sender("random@classmate.com")


def test_trusted_schedule_sender_open_when_unconfigured():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = ""
        mock_settings.return_value.require_trusted_schedule_sender = False
        mock_settings.return_value.render_url = ""
        assert is_trusted_schedule_sender("anyone@spam.com")


def test_trusted_schedule_sender_denied_in_production_without_allowlist():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = ""
        mock_settings.return_value.require_trusted_schedule_sender = True
        mock_settings.return_value.render_url = "https://mailbot.onrender.com"
        assert not is_trusted_schedule_sender("anyone@spam.com")


def test_schedule_queries_prefix_from_clause():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = "registrar@college.edu"
        queries = _schedule_queries()
    assert all("from:registrar@college.edu" in q for q in queries)


def test_digest_window_blocks_late_cron():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from services.timeutil import digest_window_open

    ist = ZoneInfo("Asia/Kolkata")
    morning = datetime(2026, 9, 22, 7, 30, tzinfo=ist)
    late_same_day = datetime(2026, 9, 22, 11, 5, tzinfo=ist)
    after_afternoon = datetime(2026, 9, 22, 16, 5, tzinfo=ist)
    evening = datetime(2026, 9, 22, 20, 10, tzinfo=ist)
    late_evening = datetime(2026, 9, 22, 23, 40, tzinfo=ist)
    after_midnight = datetime(2026, 9, 23, 0, 10, tzinfo=ist)
    library = datetime(2026, 9, 22, 7, 20, tzinfo=ist)
    assert digest_window_open("morning", morning) is True
    assert digest_window_open("morning", late_same_day) is True
    assert digest_window_open("morning", after_afternoon) is False
    assert digest_window_open("evening", evening) is True
    assert digest_window_open("evening", late_evening) is True
    assert digest_window_open("evening", after_midnight) is False
    assert digest_window_open("evening", morning) is False
    assert digest_window_open("library", library) is True
    assert digest_window_open("library", late_same_day) is True
    assert digest_window_open("library", after_afternoon) is False


def test_newsletter_is_noise_even_when_filed_as_a_task():
    from services.watcher import _is_noise_mail

    assert _is_noise_mail(
        category="deadline",
        task="Read today's brief",
        summary="Business Standard e-paper newsletter",
        subject="RBI OMOs reduce liquidity",
        sender="Business Standard <news@business-standard.com>",
    )
    assert not _is_noise_mail(
        category="deadline",
        task="Attend CP exam",
        summary="CP exam on 24 Sep 2:15 pm in LRC Hall",
        subject="Re: CP exam",
        sender="Vinay Ch <vinay@imtnag.ac.in>",
    )


def test_processed_mail_key_namespaces_other_mailboxes():
    from services.db import processed_message_key
    from services.user_ids import OWNER_SEED_USER_ID

    with patch("services.db.multi_user_schema_ready", return_value=True):
        assert processed_message_key("abc", OWNER_SEED_USER_ID) == "abc"
        assert processed_message_key("abc", "student-1") == "student-1:abc"
    with patch("services.db.multi_user_schema_ready", return_value=False):
        assert processed_message_key("abc", "student-1") == "abc"


def test_deadline_display_drops_stale_tomorrow_note():
    display = format_deadline_display(
        "2026-08-22T18:32:00+00:00",
        "tomorrow 00:02",
    )
    assert "Aug 2026" in display
    assert "Overdue" in display
    assert "tomorrow" not in display.lower()


def test_evening_digest_hides_month_old_priority():
    from services.formatting import format_evening_digest

    tomorrow = datetime(2026, 9, 23, 20, 0, tzinfo=IST)
    stale = {
        "id": "old",
        "task": "Forward the Accenture mail",
        "category": "placement",
        "course_or_topic": "Midterm",
        "deadline_date": "2026-08-22T18:32:00+00:00",
        "deadline_text": "tomorrow 00:02",
    }
    text = format_evening_digest(
        tomorrow=tomorrow,
        classes_text="",
        overdue_items=[stale],
        tomorrow_items=[],
        older_open_count=12,
    )
    assert "Accenture" not in text
    assert "12 older open tasks" in text
    assert "tomorrow 00:02" not in text


def test_relative_deadline_display_within_48h():
    future = datetime.now(IST) + timedelta(hours=3, minutes=15)
    iso = future.isoformat()
    display = format_deadline_display(iso, None)
    assert "Due in 3h" in display
    assert "—" in display


def test_category_icons():
    assert category_icon("deadline") == CATEGORY_ICONS["deadline"]
    assert category_icon("placement") == "💼"


def test_priority_and_item_type_normalization():
    out = normalize_classification(
        {
            "is_actionable": True,
            "category": "deadline",
            "item_type": "exam",
            "priority": "high",
            "task": "Midterm exam",
            "confidence": "high",
        }
    )
    assert out["item_type"] == "exam"
    assert out["priority"] == "high"


def test_boost_priority_from_weightage():
    result = _boost_priority_from_signals(
        "Assignment",
        "This carries 20% weightage and is mandatory.",
        {"priority": "normal", "category": "deadline"},
    )
    assert result["priority"] == "high"


def test_reminder_tiers_for_high_priority():
    assignment = {
        "id": "abc",
        "category": "deadline",
        "priority": "high",
        "deadline_date": (datetime.now(IST) + timedelta(days=2)).isoformat(),
    }
    assert _tiers_for_assignment(assignment) == PRIORITY_TIERS["high"]


def test_schedule_deadline_reminders_skips_past_tiers(monkeypatch):
    inserted = []

    def fake_insert(rows):
        inserted.extend(rows)
        return rows

    monkeypatch.setattr("services.reminders.insert_scheduled_reminders", fake_insert)
    assignment = {
        "id": "abc-123",
        "category": "other",
        "priority": "low",
        "deadline_date": (datetime.now(IST) + timedelta(minutes=20)).isoformat(),
    }
    count = schedule_deadline_reminders(assignment)
    assert count == 0
    assert inserted == []
