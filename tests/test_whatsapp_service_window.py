"""WhatsApp 24h window + template nudge selection."""

from datetime import datetime, timedelta, timezone

from services.messaging.pending_digest import nudge_body_parameter, nudge_template_name
from services.messaging.service_window import service_window_open, touch_service_window


def test_service_window_open_after_inbound():
    data = {}
    assert not service_window_open(data)
    touch_service_window(data)
    assert service_window_open(data)


def test_service_window_closed_after_25h():
    data = {
        "last_whatsapp_inbound_at": (
            datetime.now(timezone.utc) - timedelta(hours=25)
        ).isoformat()
    }
    assert not service_window_open(data)


def test_nudge_template_prefers_digest_for_evening():
    from unittest.mock import patch

    from config import Settings

    settings = Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
        whatsapp_alert_template="mailbot_alert_v1",
        whatsapp_digest_template="mailbot_digest_v1",
    )
    with patch("services.messaging.pending_digest.get_settings", return_value=settings):
        assert nudge_template_name("evening") == "mailbot_digest_v1"
        assert nudge_template_name("alert") == "mailbot_alert_v1"
        assert nudge_body_parameter("mailbot_digest_v1", "evening") is None
        assert "digest" in (nudge_body_parameter("mailbot_alert_v1", "alert") or "").lower()
