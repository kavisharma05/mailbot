from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_owner_chat_id: str
    telegram_webhook_secret: str
    telegram_chat_deadlines: str = ""
    telegram_chat_placements: str = ""
    telegram_chat_admin: str = ""
    telegram_chat_other: str = ""

    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"
    # Optional second Groq free account for rotation
    groq_api_key_2: str = ""

    # Fallback when Groq hits rate limits (https://cloud.cerebras.ai)
    cerebras_api_key: str = ""
    cerebras_model: str = "gemma-4-31b"

    # Free provider pool extras
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    github_models_token: str = ""
    github_models_model: str = "openai/gpt-4o-mini"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"

    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_user: str

    supabase_url: str
    supabase_service_key: str

    task_secret: str
    student_name: str = "[Your Name]"

    render_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
