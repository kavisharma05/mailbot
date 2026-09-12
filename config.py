from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_owner_chat_id: str
    # Telegram user id for owner-only commands (defaults to owner chat id for DMs)
    telegram_owner_user_id: str = ""
    telegram_webhook_secret: str
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

    # Web OAuth redirect URI for one-tap Gmail reconnect (e.g. https://app.onrender.com/oauth/callback)
    gmail_oauth_redirect_uri: str = ""

    # When true and admin_schedule_sender is blank, reject untrusted timetable mail
    require_trusted_schedule_sender: bool = True

    def owner_user_id(self) -> str:
        return (self.telegram_owner_user_id or self.telegram_owner_chat_id).strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
