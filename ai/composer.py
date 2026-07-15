from typing import Any

from ai.client import call_json
from ai.prompts import COMPOSER_PROMPT
from config import get_settings


def compose_email(instructions: str) -> dict[str, Any] | None:
    settings = get_settings()
    system = COMPOSER_PROMPT.format(student_name=settings.student_name)
    user = f"Student instructions:\n{instructions}"
    return call_json(system, user)
