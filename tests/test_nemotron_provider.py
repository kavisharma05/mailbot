"""Nemotron 3 Ultra provider configuration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import ai.client as client_module
from ai.client import _call_nemotron, _provider_chain, call_json
from config import Settings


def test_provider_chain_ordering_per_mode():
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
        text_names = [name for name, _ in _provider_chain(json_mode=False)]
        json_names = [name for name, _ in _provider_chain(json_mode=True)]
    # Text mode: Nemotron brain leads for richer Q&A answers.
    assert text_names[0] == "nemotron"
    assert "groq" in text_names
    # JSON mode: strict-formatting non-reasoning models lead for reliability.
    assert json_names[0] == "groq"
    assert "nemotron" in json_names
    assert json_names.index("groq") < json_names.index("nemotron")


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


def _settings(**overrides) -> Settings:
    # Explicitly blank every provider key so pydantic-settings does NOT pull
    # real keys from the local .env file (which would make live API calls).
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
    )
    base.update(overrides)
    return Settings(**base)


def test_reasoning_only_response_is_not_served_as_answer():
    """Empty content + non-empty reasoning must be treated as failure, not answer."""
    client_module._cooldowns.clear()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [
            {"message": {"content": "", "reasoning": "The user said ping. I should..."}}
        ]
    }
    with patch("ai.client.httpx.post", return_value=fake_resp):
        out = client_module._openai_compatible_post(
            url="https://example.invalid/v1/chat/completions",
            api_key="k",
            model="m",
            system="s",
            user="u",
            json_mode=False,
            max_tokens=50,
            temperature=0.0,
            provider="testprov",
        )
    assert out is None
    client_module._cooldowns.clear()


def test_call_json_falls_through_when_first_provider_returns_non_json():
    """A provider returning garbage for a JSON call must not poison the request."""
    client_module._cooldowns.clear()
    settings = _settings(nemotron_api_key="n-key")

    # JSON chain order is groq first, nemotron later — so groq serves garbage
    # and nemotron must rescue the call.
    def fake_groq(system, user, *, json_mode, max_tokens, temperature, **_kw):
        return "Sure! Here is your email draft, hope this helps..."  # not JSON

    def fake_nemotron(system, user, *, json_mode, max_tokens, temperature, **_kw):
        return '{"subject": "Hi", "body": "Hello"}'

    with (
        patch("ai.client.get_settings", return_value=settings),
        patch("ai.client._call_nemotron", side_effect=fake_nemotron),
        patch("ai.client._call_groq", side_effect=fake_groq),
        patch("ai.client._is_cooled", return_value=False),
    ):
        result = call_json("system", "user")
    assert result == {"subject": "Hi", "body": "Hello"}
    client_module._cooldowns.clear()


def test_call_json_returns_none_when_all_providers_unusable():
    client_module._cooldowns.clear()
    settings = _settings(nemotron_api_key="n-key")

    with (
        patch("ai.client.get_settings", return_value=settings),
        patch("ai.client._call_nemotron", lambda *a, **kw: "still not json"),
        patch("ai.client._call_groq", lambda *a, **kw: None),
        patch("ai.client._is_cooled", return_value=False),
    ):
        assert call_json("system", "user") is None
    client_module._cooldowns.clear()
