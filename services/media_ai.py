"""Understand inbound user images and audio notes with configured AI providers."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(90.0)
_MAX_IMAGE_BYTES = 15 * 1024 * 1024
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_VISION_PROMPT = (
    "Analyze this image accurately. Extract all visible text, handwriting, dates, "
    "names, amounts, tables, and important visual details. Then explain what the "
    "image contains and answer the user's caption/request if provided. Do not "
    "invent unreadable details."
)


def _response_text(data: dict[str, Any]) -> str | None:
    parts = (
        ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")
        or []
    )
    text = "\n".join(
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()
    return text or None


def analyze_image(
    data: bytes,
    *,
    mime_type: str = "image/jpeg",
    caption: str | None = None,
) -> str | None:
    """Return a faithful text description/OCR of an image using Gemini vision."""
    settings = get_settings()
    if not data or len(data) > _MAX_IMAGE_BYTES or not settings.gemini_api_key:
        return None
    prompt = _VISION_PROMPT
    if caption and caption.strip():
        prompt += f"\n\nUser caption/request: {caption.strip()}"
    model = (settings.gemini_model or "gemini-2.5-flash").strip()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={settings.gemini_api_key}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type or "image/jpeg",
                            "data": base64.b64encode(data).decode("ascii"),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1800},
    }
    try:
        resp = httpx.post(url, json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Gemini image analysis failed %s: %s", resp.status_code, resp.text[:300])
            return None
        return _response_text(resp.json())
    except Exception as exc:
        logger.warning("Gemini image analysis exception: %s", exc)
        return None


def transcribe_audio(
    data: bytes,
    *,
    filename: str = "voice.ogg",
    mime_type: str = "audio/ogg",
) -> str | None:
    """Transcribe an inbound audio/voice message through Groq Whisper."""
    settings = get_settings()
    if not data or len(data) > _MAX_AUDIO_BYTES:
        return None
    keys = [key for key in (settings.groq_api_key, settings.groq_api_key_2) if key]
    if not keys:
        return None
    safe_name = filename or "voice.ogg"
    for key in keys:
        try:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (safe_name, data, mime_type or "application/octet-stream")},
                data={
                    "model": "whisper-large-v3-turbo",
                    "response_format": "json",
                    "temperature": "0",
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code >= 400:
                logger.warning("Groq transcription failed %s: %s", resp.status_code, resp.text[:300])
                continue
            text = str(resp.json().get("text") or "").strip()
            if text:
                return text
        except Exception as exc:
            logger.warning("Groq transcription exception: %s", exc)
    return None
