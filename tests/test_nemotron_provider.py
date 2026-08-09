"""Nemotron 3 Ultra provider configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ai.client as client_module
from ai.client import _call_nemotron, _provider_chain
from config import Settings


def test_nemotron_first_in_provider_chain():
    settings = Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        nemotron_api_key="n",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
    )
    with patch("ai.client.get_settings", return_value=settings):
        names = [name for name, _ in _provider_chain()]
    assert names[0] == "nemotron"
    assert "groq" in names


def test_nemotron_uses_primary_model_for_json_mode():
    settings = Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        nemotron_api_key="n-key",
        nemotron_primary_model="nvidia/nemotron-3-ultra-550b-a55b:free",
        nemotron_brain_model="nvidia/nemotron-3-ultra-550b-a55b:free",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
    )
    client_module._cooldowns.clear()
    with (
        patch("ai.client.get_settings", return_value=settings),
        patch("ai.client._openai_compatible_post", return_value='{"ok": true}') as post,
    ):
        result = _call_nemotron(
            "system",
            "user",
            json_mode=True,
            max_tokens=100,
            temperature=0.2,
        )
    assert result == '{"ok": true}'
    assert post.call_args.kwargs["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert post.call_args.kwargs["provider"] == "nemotron"
    client_module._cooldowns.clear()


def test_nemotron_uses_brain_model_for_text_mode():
    settings = Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        nemotron_api_key="n-key",
        nemotron_primary_model="primary-model",
        nemotron_brain_model="brain-model",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
    )
    client_module._cooldowns.clear()
    with (
        patch("ai.client.get_settings", return_value=settings),
        patch("ai.client._openai_compatible_post", return_value="answer") as post,
    ):
        result = _call_nemotron(
            "system",
            "user",
            json_mode=False,
            max_tokens=100,
            temperature=0.5,
        )
    assert result == "answer"
    assert post.call_args.kwargs["model"] == "brain-model"
    client_module._cooldowns.clear()
