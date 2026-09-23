from typing import Any

from ai.client import call_json
from ai.prompts import COMPOSER_PROMPT


def compose_email(instructions: str) -> dict[str, Any] | None:
    from services.mailbox_user import student_name

    system = COMPOSER_PROMPT.format(student_name=student_name())
    user = f"Student instructions:\n{instructions}"
    return call_json(system, user)
