"""Settings for the IMT Access integration.

Reads from the same .env / environment as the rest of Mailbot (config.py).
Kept as a small standalone dataclass so the CLI can run without importing the
whole FastAPI app.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_LOADED = False

# Short-lived cache for the Supabase-stored credential fallback so we don't
# hit PostgREST on every settings read.
_config_cache: tuple[float, dict[str, str]] | None = None
_CONFIG_CACHE_TTL = 300.0


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    _ENV_LOADED = True


@dataclass(frozen=True)
class IMTSettings:
    mapmyaccess_email: str = ""
    mapmyaccess_password: str = ""
    mapmyaccess_base_url: str = "https://imtnagpur.mapmyaccess.com"
    imt_daily_enabled: bool = True
    # Add a short LLM-written intro to the daily digest when a provider is up
    imt_llm_summaries: bool = True
    # Max headlines per source in the daily digest
    imt_headlines_per_source: int = 8
    # Re-login when the cached session token expires in less than N seconds
    imt_session_refresh_margin: int = 3600

    @property
    def configured(self) -> bool:
        return bool(self.mapmyaccess_email and self.mapmyaccess_password)


def _portal_config_fallback() -> dict[str, str]:
    """Read portal credentials stored in Supabase (kind='portal_config').

    Lets the deployed service run without MAPMYACCESS_* env vars — handy when
    the hosting dashboard is not accessible. Env vars always win when set.
    """
    global _config_cache
    now = time.time()
    if _config_cache and now - _config_cache[0] < _CONFIG_CACHE_TTL:
        return _config_cache[1]
    try:
        from imt_access import store

        config = store.get_portal_config()
    except Exception:
        config = {}
    _config_cache = (now, config)
    return config


def get_imt_settings() -> IMTSettings:
    _ensure_env()

    def _bool(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    email = (os.environ.get("MAPMYACCESS_EMAIL") or "").strip()
    password = os.environ.get("MAPMYACCESS_PASSWORD") or ""
    if not email or not password:
        fallback = _portal_config_fallback()
        email = email or fallback.get("mapmyaccess_email", "")
        password = password or fallback.get("mapmyaccess_password", "")

    return IMTSettings(
        mapmyaccess_email=email,
        mapmyaccess_password=password,
        mapmyaccess_base_url=(
            os.environ.get("MAPMYACCESS_BASE_URL") or "https://imtnagpur.mapmyaccess.com"
        ).strip().rstrip("/"),
        imt_daily_enabled=_bool("IMT_DAILY_ENABLED", True),
        imt_llm_summaries=_bool("IMT_LLM_SUMMARIES", True),
        imt_headlines_per_source=_int("IMT_HEADLINES_PER_SOURCE", 8),
        imt_session_refresh_margin=_int("IMT_SESSION_REFRESH_MARGIN", 3600),
    )
