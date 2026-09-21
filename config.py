from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_owner_chat_id: str
    # Telegram user id for owner-only commands (defaults to owner chat id for DMs)
    telegram_owner_user_id: str = ""
    telegram_webhook_secret: str
    # WhatsApp Cloud API (Meta) — optional second channel alongside Telegram
    whatsapp_enabled: bool = False
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_owner_phone: str = ""
    whatsapp_api_version: str = "v22.0"
    whatsapp_alert_template: str = ""
    whatsapp_digest_template: str = ""
    whatsapp_waba_id: str = ""
    telegram_chat_deadlines: str = ""
    telegram_chat_placements: str = ""
    telegram_chat_certification: str = ""
    telegram_chat_admin: str = ""
    telegram_chat_other: str = ""

    # When true, owner gets a DM for every classified email (including newsletters).
    # Default false — only actionable / high-priority mail DMs the owner.
    owner_dm_all_mail: bool = False

    # Default hours for REMIND AGAIN on actionable alerts
    remind_again_hours: int = 4

    groq_api_key: str
    groq_model: str = "openai/gpt-oss-120b"
    # Optional second Groq free account for rotation
    groq_api_key_2: str = ""

    # Nemotron 3 Ultra (free) via OpenRouter — primary + brain when configured
    # https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b:free
    nemotron_api_key: str = ""
    nemotron_primary_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"
    nemotron_brain_model: str = "nvidia/nemotron-3-ultra-550b-a55b:free"

    # Fallback when Groq hits rate limits (https://cloud.cerebras.ai)
    cerebras_api_key: str = ""
    cerebras_model: str = "gpt-oss-120b"

    # Free provider pool extras
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    github_models_token: str = ""
    github_models_model: str = "openai/gpt-4o-mini"
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemma-4-31b-it:free"

    # Optional Claude.ai browser-session provider (account 1 only for now).
    # This is additive: existing free providers stay configured and are used as fallbacks.
    claude_session_enabled: bool = False
    claude_session_label: str = ""
    claude_org_uuid: str = ""
    claude_account_uuid: str = ""
    claude_email: str = ""
    claude_session_key: str = ""
    claude_cookie_header: str = ""
    claude_model: str = "claude-sonnet-4-6"

    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_user: str

    supabase_url: str
    supabase_service_key: str

    task_secret: str
    student_name: str = "[Your Name]"

    render_url: str = ""

    # healthchecks.io ping URL (no-op when blank)
    heartbeat_url: str = ""

    # Comma-separated trusted senders for timetable mail (e.g. registrar@mycollege.ac.in)
    admin_schedule_sender: str = ""

    # Extra institutional domains treated as official faculty mail (comma-separated)
    official_email_domains: str = ""

    # Web OAuth redirect URI for one-tap Gmail reconnect (e.g. https://app.onrender.com/oauth/callback)
    gmail_oauth_redirect_uri: str = ""

    # When true and admin_schedule_sender is blank, reject untrusted timetable mail
    require_trusted_schedule_sender: bool = True

    # IMT Nagpur MapMyAccess e-resources portal (daily library digest)
    mapmyaccess_email: str = ""
    mapmyaccess_password: str = ""
    mapmyaccess_base_url: str = "https://imtnagpur.mapmyaccess.com"
    imt_daily_enabled: bool = True
    imt_llm_summaries: bool = True
    imt_headlines_per_source: int = 8

    def owner_user_id(self) -> str:
        return (self.telegram_owner_user_id or self.telegram_owner_chat_id).strip()


_CLAUDE_SESSION_FILE = Path(__file__).resolve().with_name(".env.claude_session.account1")
_CLAUDE_SESSION_FIELDS = {
    "CLAUDE_SESSION_ENABLED": "claude_session_enabled",
    "CLAUDE_SESSION_LABEL": "claude_session_label",
    "CLAUDE_ORG_UUID": "claude_org_uuid",
    "CLAUDE_ACCOUNT_UUID": "claude_account_uuid",
    "CLAUDE_EMAIL": "claude_email",
    "CLAUDE_SESSION_KEY": "claude_session_key",
    "CLAUDE_COOKIE_HEADER": "claude_cookie_header",
    "CLAUDE_MODEL": "claude_model",
}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Keep the browser-session export out of the normal Settings constructor so
    # tests and scripts that build isolated Settings objects never pick it up.
    if _CLAUDE_SESSION_FILE.exists():
        values = dotenv_values(_CLAUDE_SESSION_FILE)
        enabled = str(values.get("CLAUDE_SESSION_ENABLED") or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if enabled:
            for env_name, field_name in _CLAUDE_SESSION_FIELDS.items():
                value = values.get(env_name)
                if value in (None, ""):
                    continue
                if field_name == "claude_session_enabled":
                    value = enabled
                setattr(settings, field_name, value)
    return settings
