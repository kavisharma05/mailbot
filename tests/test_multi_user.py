"""Multi-user helpers (no live Supabase required)."""

import hashlib
from unittest.mock import patch

from gmail_api.auth import active_gmail_user_id
from services.db import resolve_user_id
from services.mailbox_user import gmail_address, reset, set_user, student_name
from services.oauth_state import create_oauth_state, verify_oauth_state
from services.user_ids import OWNER_SEED_USER_ID
from services.users import _normalize_username


def test_normalize_username():
    assert _normalize_username("@Alice") == "alice"


def test_oauth_state_roundtrip():
    state = create_oauth_state(OWNER_SEED_USER_ID)
    ok, uid = verify_oauth_state(state)
    assert ok is True
    assert uid == OWNER_SEED_USER_ID


def test_unbound_request_stays_on_owner():
    assert resolve_user_id(None) == OWNER_SEED_USER_ID
    assert active_gmail_user_id() == OWNER_SEED_USER_ID


def test_student_request_uses_their_mailbox_and_name():
    token = set_user(
        {
            "id": "student-1",
            "role": "student",
            "display_name": "rahul",
            "gmail_address": "2026204@imtnag.ac.in",
        }
    )
    try:
        assert resolve_user_id(None) == "student-1"
        assert active_gmail_user_id() == "student-1"
        assert gmail_address() == "2026204@imtnag.ac.in"
        assert student_name() == "Rahul"
    finally:
        reset(token)


def test_student_without_gmail_does_not_inherit_owner_address():
    token = set_user(
        {
            "id": "student-2",
            "role": "student",
            "display_name": "Student",
            "gmail_address": "",
        }
    )
    try:
        with patch("services.mailbox_user.get_settings") as settings:
            settings.return_value.gmail_user = "owner@gmail.com"
            settings.return_value.student_name = "Kavish Sharma"
            assert gmail_address() == ""
            assert student_name() == "Student"
    finally:
        reset(token)


def test_dashboard_name_comes_from_gmail_when_handle_is_stored():
    from services.users import stored_name_should_come_from_gmail

    assert stored_name_should_come_from_gmail(
        {
            "gmail_address": "2026204@imtnag.ac.in",
            "display_name": "2026204",
            "telegram_username": "2026204",
        }
    )
    assert stored_name_should_come_from_gmail(
        {
            "gmail_address": "rahul@imtnag.ac.in",
            "display_name": "rahul",
            "telegram_username": "rahul",
        }
    )
    assert not stored_name_should_come_from_gmail(
        {
            "gmail_address": "rahul@imtnag.ac.in",
            "display_name": "Rahul Sharma",
            "telegram_username": "rahul",
        }
    )
    assert not stored_name_should_come_from_gmail(
        {"gmail_address": "", "display_name": "rahul", "telegram_username": "rahul"}
    )


def test_login_code_hash_stable():
    raw = "123456"
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == hashlib.sha256(b"123456").hexdigest()
