from datetime import datetime
from typing import Any

from ai.client import call_json
from ai.prompts import DEADLINE_PROMPT


def classify_email(subject: str, body: str, sender: str) -> dict[str, Any] | None:
    today = datetime.now().strftime("%Y-%m-%d")
    system = DEADLINE_PROMPT.format(today=today)
    user = (
        f"From: {sender}\n"
        f"Subject: {subject}\n\n"
        f"{body}"
    )
    return call_json(system, user)
