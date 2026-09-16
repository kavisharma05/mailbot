"""Faculty official-mail class schedule changes (cancel / venue / extra)."""

from datetime import date
from unittest.mock import patch

from services.schedule import (
    apply_schedule_changes,
    is_official_faculty_sender,
    looks_like_full_timetable,
    looks_like_schedule_change,
    merge_session_overlays,
    overlay_kind,
)


def test_official_faculty_sender_accepts_ac_in_not_gmail():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = ""
        mock_settings.return_value.require_trusted_schedule_sender = False
        mock_settings.return_value.render_url = ""
        mock_settings.return_value.official_email_domains = ""
        assert is_official_faculty_sender("Dr. Rao <rao@imtnag.ac.in>")
        assert is_official_faculty_sender("Prof <prof@iimb.ac.in>")
        assert not is_official_faculty_sender("Friend <someone@gmail.com>")
        assert not is_official_faculty_sender("Yahoo <x@yahoo.com>")


def test_official_faculty_sender_extra_domains():
    with patch("services.schedule.get_settings") as mock_settings:
        mock_settings.return_value.admin_schedule_sender = ""
        mock_settings.return_value.require_trusted_schedule_sender = True
        mock_settings.return_value.render_url = "https://app.example.com"
        mock_settings.return_value.official_email_domains = "imtnag.ac.in, college.org"
        assert is_official_faculty_sender("Faculty <sk@college.org>")
        assert not is_official_faculty_sender("Personal <sk@gmail.com>")


def test_looks_like_schedule_change_cancel_and_venue():
    assert looks_like_schedule_change(
        "OB class cancelled",
        "Tomorrow's OB class is cancelled. It will not be held.",
    )
    assert looks_like_schedule_change(
        "Venue change",
        "The Marketing class is shifted to Arthum #4.",
    )
    assert looks_like_schedule_change(
        "Extra class",
        "There will be an extra class for Finance on Friday at 2pm.",
    )


def test_looks_like_schedule_change_ignores_full_timetable():
    pdf = "Mon 20.07.26 timetable " + ("Arthum " * 80)
    assert looks_like_full_timetable("MBA Term I timetable", "Please find attached", pdf)
    assert not looks_like_schedule_change(
        "MBA Term I timetable",
        "Please find attached the weekly timetable",
        pdf,
        {"is_schedule": True},
    )
    assert not looks_like_schedule_change(
        "Registration reminder",
        "Please complete your registration form by Friday.",
    )


def test_merge_cancel_venue_and_extra():
    projected = [
        {
            "course_or_topic": "OB - Sec. C",
            "session_date": "2026-09-17",
            "start_time": "09:45",
            "end_time": "11:15",
            "room": "Arthum#2",
        },
        {
            "course_or_topic": "Marketing",
            "session_date": "2026-09-17",
            "start_time": "14:00",
            "end_time": "15:30",
            "room": "AEC Hall",
        },
    ]
    overlays = [
        {
            "course_or_topic": "OB",
            "session_date": "2026-09-17",
            "start_time": "09:45",
            "room": "Arthum#2",
            "notes": "[CANCELLED] Faculty unavailable",
        },
        {
            "course_or_topic": "Marketing",
            "session_date": "2026-09-17",
            "start_time": "14:00",
            "room": "Tiered#1",
            "notes": "[VENUE] Moved to Tiered#1",
        },
        {
            "course_or_topic": "Finance",
            "session_date": "2026-09-17",
            "start_time": "18:00",
            "end_time": "19:30",
            "room": "Arthum#3",
            "notes": "[ADDED] Extra class",
        },
    ]
    merged = merge_session_overlays(projected, overlays)
    by_course = {s["course_or_topic"]: s for s in merged}
    assert by_course["OB - Sec. C"]["cancelled"] is True
    assert by_course["OB - Sec. C"]["change"] == "cancelled"
    assert by_course["Marketing"]["room"] == "Tiered#1"
    assert by_course["Marketing"]["change"] == "venue"
    assert by_course["Finance"]["change"] == "added"
    assert overlay_kind("[CANCELLED] x") == "cancelled"


def test_apply_schedule_changes_writes_overlays_not_timetable():
    template = [
        {
            "course_or_topic": "OB",
            "session_date": "2026-09-17",
            "start_time": "09:45",
            "end_time": "11:15",
            "room": "Arthum#2",
            "week_start": "2026-09-14",
        }
    ]
    changes = [
        {
            "action": "cancel",
            "course_or_topic": "OB",
            "session_date": "2026-09-17",
            "start_time": "09:45",
            "end_time": "11:15",
            "room": None,
            "new_session_date": None,
            "new_start_time": None,
            "new_end_time": None,
            "new_room": None,
            "notes": "Faculty unavailable",
        },
        {
            "action": "add",
            "course_or_topic": "Finance",
            "session_date": "2026-09-18",
            "start_time": "14:00",
            "end_time": "15:30",
            "room": None,
            "new_session_date": None,
            "new_start_time": None,
            "new_end_time": None,
            "new_room": "Arthum#4",
            "notes": "Make-up class",
        },
    ]
    with (
        patch("services.schedule.delete_class_sessions_for_message") as mock_delete,
        patch("services.schedule.insert_class_sessions", side_effect=lambda rows: rows) as mock_insert,
    ):
        rows = apply_schedule_changes(
            changes,
            gmail_message_id="msg-change-1",
            faculty="Dr. Rao",
            template=template,
        )
    mock_delete.assert_called_once_with("msg-change-1")
    mock_insert.assert_called_once()
    kinds = {overlay_kind(r["notes"]) for r in rows}
    assert kinds == {"cancelled", "added"}
    assert all(r["gmail_message_id"] == "msg-change-1" for r in rows)
    added = next(r for r in rows if overlay_kind(r["notes"]) == "added")
    assert added["session_date"] == "2026-09-18"
    assert added["room"] == "Arthum#4"


def test_sessions_for_day_applies_overlays(monkeypatch):
    from services import schedule as sch

    template = [
        {
            "course_or_topic": "OB",
            "session_date": date(2026, 9, 14).isoformat(),  # Monday
            "start_time": "09:45",
            "end_time": "11:15",
            "room": "Arthum#2",
        },
        {
            "course_or_topic": "Marketing",
            "session_date": date(2026, 9, 15).isoformat(),  # Tuesday
            "start_time": "14:00",
            "end_time": "15:30",
            "room": "AEC Hall",
        },
    ]
    tuesday = date(2026, 9, 22)  # next Tuesday, projected
    monkeypatch.setattr(
        sch,
        "overlays_for_day",
        lambda day: [
            {
                "course_or_topic": "Marketing",
                "session_date": day.isoformat(),
                "start_time": "14:00",
                "room": "Tiered#3",
                "notes": "[VENUE] Room change",
            }
        ]
        if day == tuesday
        else [],
    )
    sessions = sch.sessions_for_day(tuesday, template=template)
    assert len(sessions) == 1
    assert sessions[0]["room"] == "Tiered#3"
    assert sessions[0]["change"] == "venue"
