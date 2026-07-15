import json
import logging
import time
from typing import Any

from groq import APIStatusError, Groq

from config import get_settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 4
_RETRY_STATUS = {429, 500, 502, 503}


def call_json(system: str, user: str, model: str | None = None) -> dict[str, Any] | None:
    """Call Groq and parse strict JSON from the response.

    Retries transient errors (rate limits / overload) with exponential
    backoff. Returns None on unrecoverable failure; callers handle None.
    """
    settings = get_settings()
    client = Groq(api_key=settings.groq_api_key)
    resolved_model = model or settings.groq_model

    resp = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(
                model=resolved_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                max_tokens=1024,
                temperature=0.3,
            )
            break
        except APIStatusError as exc:
            if exc.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                delay = 2 ** (attempt - 1)
                logger.warning(
                    "Groq transient error %s (attempt %d/%d), retrying in %ds",
                    exc.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
                continue
            logger.error("Groq call failed: %s", exc)
            return None

    if resp is None:
        return None

    text = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON from Groq response: %s", text[:200])
        return None
