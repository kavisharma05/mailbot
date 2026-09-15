"""Optional Claude.ai browser-session provider."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ai.client as client_module
from ai.client import _call_claude_session, _extract_claude_stream_text, _provider_chain
from config import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        groq_api_key_2="",
        nemotron_api_key="",
        cerebras_api_key="",
        gemini_api_key="",
        github_models_token="",
        openrouter_api_key="",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
        claude_session_enabled=False,
        claude_org_uuid="",
        claude_session_key="",
        claude_cookie_header="",
    )
    base.update(overrides)
    return Settings(**base)


def test_provider_chain_omits_claude_until_enabled():
    disabled = _settings()
    enabled = _settings(
        claude_session_enabled=True,
        claude_org_uuid="org-1",
        claude_session_key="session-secret",
    )
    with patch("ai.client.get_settings", return_value=disabled):
        assert "claude_session" not in [name for name, _ in _provider_chain()]
    with patch("ai.client.get_settings", return_value=enabled):
        assert [name for name, _ in _provider_chain()][0] == "claude_session"


def test_extract_claude_stream_text_reads_content_deltas():
    body = "\n".join(
        [
            'data: {"type":"message_start","message":{"content":[]}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" world"}}',
            'data: {"type":"message_stop"}',
        ]
    )
    assert _extract_claude_stream_text(body) == "Hello world"


def test_call_claude_session_creates_completes_and_cleans_up():
    settings = _settings(
        claude_session_enabled=True,
        claude_org_uuid="org-1",
        claude_session_key="session-secret",
        claude_cookie_header="anthropic-device-id=device-1",
        claude_model="claude-sonnet-4-5",
    )
    created = MagicMock(status_code=200)
    created.json.return_value = {"uuid": "conv-1"}
    completed = MagicMock(status_code=200)
    completed.text = 'data: {"type":"content_block_delta","delta":{"text":"OK"}}\n'
    deleted = MagicMock(status_code=200)
    request = MagicMock(side_effect=[created, completed, deleted])

    client_module._cooldowns.clear()
    with (
        patch("ai.client.get_settings", return_value=settings),
        patch("ai.client._claude_request", request),
    ):
        result = _call_claude_session(
            "system",
            "user",
            json_mode=True,
            max_tokens=100,
            temperature=0.2,
        )

    assert result == "OK"
    create_call = request.call_args_list[0]
    complete_call = request.call_args_list[1]
    assert create_call.args[0] == "POST"
    assert "sessionKey=session-secret" in create_call.kwargs["headers"]["Cookie"]
    assert complete_call.kwargs["json"]["model"] == "claude-sonnet-4-5"
    assert "strict JSON" in complete_call.kwargs["json"]["prompt"]
    assert request.call_args_list[2].args[0] == "DELETE"
    client_module._cooldowns.clear()
