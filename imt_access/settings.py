"""Settings for the IMT Access integration.

Reads from the same .env / environment as the rest of Mailbot (config.py).
Kept as a small standalone dataclass so the CLI can run without importing the
whole FastAPI app.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_LOADED = False


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
    # Max EPW article PDFs to attach to the daily digest (0 = skip PDF attempt)
    imt_epw_pdf_count: int = 2
    # Add a short LLM-written intro to the daily digest when a provider is up
    imt_llm_summaries: bool = True
    # Max headlines per source in the daily digest
    imt_headlines_per_source: int = 8
    # Re-login when the cached session token expires in less than N seconds
    imt_session_refresh_margin: int = 3600

    @property
    def configured(self) -> bool:
        return bool(self.mapmyaccess_email and self.mapmyaccess_password)


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

    return IMTSettings(
        mapmyaccess_email=(os.environ.get("MAPMYACCESS_EMAIL") or "").strip(),
        mapmyaccess_password=os.environ.get("MAPMYACCESS_PASSWORD") or "",
        mapmyaccess_base_url=(
            os.environ.get("MAPMYACCESS_BASE_URL") or "https://imtnagpur.mapmyaccess.com"
        ).strip().rstrip("/"),
        imt_daily_enabled=_bool("IMT_DAILY_ENABLED", True),
        imt_epw_pdf_count=_int("IMT_EPW_PDF_COUNT", 2),
        imt_llm_summaries=_bool("IMT_LLM_SUMMARIES", True),
        imt_headlines_per_source=_int("IMT_HEADLINES_PER_SOURCE", 8),
        imt_session_refresh_margin=_int("IMT_SESSION_REFRESH_MARGIN", 3600),
    )
