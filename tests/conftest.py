"""Shared test isolation for services backed by production infrastructure."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def legacy_single_user_test_mode():
    """Keep existing endpoint/handler tests independent of the live database.

    Dedicated multi-user tests patch the user service explicitly. The remaining
    suite predates web authentication and should exercise application behavior,
    not whichever schema happens to be deployed in Supabase.
    """
    with (
        patch("services.db.multi_user_schema_ready", return_value=False),
        patch("bot.handlers._require_user", return_value={"id": "test-owner", "role": "owner"}),
    ):
        yield
