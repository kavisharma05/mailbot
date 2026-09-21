"""Multi-user helpers (no live Supabase required)."""

import hashlib

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


def test_login_code_hash_stable():
    raw = "123456"
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == hashlib.sha256(b"123456").hexdigest()
