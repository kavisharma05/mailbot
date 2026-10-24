"""Shared test isolation for services backed by production infrastructure."""

import os

# GitHub Actions has no .env — set required Settings fields before app imports.
for _key, _val in {
    "TELEGRAM_BOT_TOKEN": "123456789:AAHdFhoAAAAExampleTestTokenForPytestOnly",
    "TELEGRAM_OWNER_CHAT_ID": "1",
    "TELEGRAM_WEBHOOK_SECRET": "test-webhook-secret",
    "GROQ_API_KEY": "test-groq",
    "GMAIL_CLIENT_ID": "test-client-id",
    "GMAIL_CLIENT_SECRET": "test-client-secret",
    "GMAIL_REFRESH_TOKEN": "test-refresh",
    "GMAIL_USER": "test@example.com",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-service-key",
    "TASK_SECRET": "test-task-secret",
}.items():
    os.environ.setdefault(_key, _val)

from unittest.mock import patch

import pytest


_OWNER_ROW = {"id": "test-owner", "role": "owner", "telegram_id": "1"}


@pytest.fixture(autouse=True)
def legacy_single_user_test_mode():
    """Keep existing endpoint/handler tests independent of the live database.

    Dedicated multi-user tests patch the user service explicitly. The remaining
    suite predates web authentication and should exercise application behavior,
    not whichever schema happens to be deployed in Supabase.
    """
    with (
        patch("services.db.multi_user_schema_ready", return_value=False),
        patch("bot.handlers._require_user", return_value=_OWNER_ROW),
        patch("services.users.get_owner_user", return_value=_OWNER_ROW),
        patch("services.users.ensure_owner_user", return_value=_OWNER_ROW),
    ):
        yield
