from services.user_ids import OWNER_SEED_USER_ID
from services.watcher import watcher_result_auth_failed


def test_watcher_result_auth_failed_single_user():
    assert watcher_result_auth_failed({"status": "auth_failed"})


def test_watcher_result_auth_failed_all_users():
    assert watcher_result_auth_failed(
        {
            "status": "multi",
            "users": [
                {"status": "auth_failed", "user_id": "a"},
                {"status": "auth_failed", "user_id": "b"},
            ],
        }
    )


def test_watcher_result_auth_failed_owner_only():
    assert watcher_result_auth_failed(
        {
            "status": "multi",
            "users": [
                {"status": "auth_failed", "user_id": OWNER_SEED_USER_ID},
                {"status": "ok", "user_id": "student"},
            ],
        }
    )


def test_watcher_result_auth_failed_partial_student_only():
    assert not watcher_result_auth_failed(
        {
            "status": "multi",
            "users": [
                {"status": "ok", "user_id": OWNER_SEED_USER_ID},
                {"status": "auth_failed", "user_id": "student"},
            ],
        }
    )
