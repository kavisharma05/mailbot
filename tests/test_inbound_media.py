"""Inbound image and voice/audio processing on Telegram and WhatsApp."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from services import media_ai


def _run(coro):
    return asyncio.run(coro)


def test_analyze_image_uses_gemini_multimodal(monkeypatch):
    settings = SimpleNamespace(gemini_api_key="g-key", gemini_model="gemini-test")
    monkeypatch.setattr(media_ai, "get_settings", lambda: settings)
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "Invoice total is ₹500."}]}}]
    }
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(media_ai.httpx, "post", fake_post)
    result = media_ai.analyze_image(
        b"jpeg-data", mime_type="image/jpeg", caption="What is the total?"
    )
    assert result == "Invoice total is ₹500."
    assert "gemini-test:generateContent" in calls[0][0]
    parts = calls[0][1]["json"]["contents"][0]["parts"]
    assert parts[1]["inline_data"]["mime_type"] == "image/jpeg"
    assert "What is the total?" in parts[0]["text"]


def test_transcribe_audio_uses_groq_whisper(monkeypatch):
    settings = SimpleNamespace(groq_api_key="g-key", groq_api_key_2="")
    monkeypatch.setattr(media_ai, "get_settings", lambda: settings)
    response = MagicMock(status_code=200)
    response.json.return_value = {"text": "What is due tomorrow?"}
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(media_ai.httpx, "post", fake_post)
    result = media_ai.transcribe_audio(
        b"ogg-data", filename="voice.ogg", mime_type="audio/ogg"
    )
    assert result == "What is due tomorrow?"
    assert calls[0][1]["data"]["model"] == "whisper-large-v3-turbo"
    assert calls[0][1]["files"]["file"] == ("voice.ogg", b"ogg-data", "audio/ogg")


def test_whatsapp_download_media(monkeypatch):
    from services.messaging import whatsapp

    settings = SimpleNamespace(
        whatsapp_access_token="token",
        whatsapp_phone_number_id="phone-id",
        whatsapp_api_version="v22.0",
        whatsapp_enabled=True,
        whatsapp_owner_phone="111",
    )
    client = whatsapp.WhatsAppClient(settings)
    metadata = MagicMock(status_code=200)
    metadata.json.return_value = {
        "url": "https://lookaside.facebook.com/media",
        "mime_type": "audio/ogg",
    }
    media = MagicMock(
        status_code=200,
        content=b"voice-bytes",
        headers={"content-type": "audio/ogg"},
    )
    fake_http = MagicMock()
    fake_http.get = AsyncMock(side_effect=[metadata, media])
    monkeypatch.setattr(whatsapp, "_http", lambda: fake_http)

    assert _run(client.download_media("media-1")) == (b"voice-bytes", "audio/ogg")
    assert fake_http.get.call_args_list[0].args[0].endswith("/v22.0/media-1")
    assert fake_http.get.call_args_list[1].args[0] == "https://lookaside.facebook.com/media"


def test_whatsapp_image_is_processed_and_replied_as_text(monkeypatch):
    from bot import whatsapp_router

    client = MagicMock()
    client.send_text = AsyncMock(return_value=True)
    client.download_media = AsyncMock(return_value=(b"image", "image/jpeg"))
    monkeypatch.setattr(whatsapp_router, "get_whatsapp_client", lambda: client)
    monkeypatch.setattr(
        whatsapp_router, "analyze_image", lambda *args, **kwargs: "Visible text: exam Friday."
    )
    user_data = {}

    _run(
        whatsapp_router._process_media(
            {
                "type": "image",
                "image": {"id": "img-1", "caption": "Read this"},
            },
            phone="111",
            message_id="m1",
            user_data=user_data,
        )
    )
    client.download_media.assert_awaited_once_with("img-1")
    sent_texts = [call.args[1] for call in client.send_text.await_args_list]
    assert sent_texts == ["Processing the image…", "Visible text: exam Friday."]


def test_whatsapp_audio_transcript_enters_text_pipeline(monkeypatch):
    from bot import whatsapp_router

    client = MagicMock()
    client.send_text = AsyncMock(return_value=True)
    client.download_media = AsyncMock(return_value=(b"voice", "audio/ogg"))
    dispatch = AsyncMock()
    monkeypatch.setattr(whatsapp_router, "get_whatsapp_client", lambda: client)
    monkeypatch.setattr(
        whatsapp_router, "transcribe_audio", lambda *args, **kwargs: "What is due tomorrow?"
    )
    monkeypatch.setattr(whatsapp_router, "_dispatch_text", dispatch)

    _run(
        whatsapp_router._process_media(
            {"type": "audio", "audio": {"id": "aud-1", "mime_type": "audio/ogg"}},
            phone="111",
            message_id="m2",
            user_data={"x": 1},
        )
    )
    dispatch.assert_awaited_once_with(
        "What is due tomorrow?", "111", "m2", {"x": 1}
    )


def test_telegram_voice_transcript_enters_chat_pipeline(monkeypatch):
    from bot import handlers

    message = MagicMock()
    message.photo = []
    message.voice = SimpleNamespace(file_id="voice-1", mime_type="audio/ogg")
    message.audio = None
    message.reply_text = AsyncMock()
    update = MagicMock(message=message)
    context = MagicMock()
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(b"voice"))
    context.bot.get_file = AsyncMock(return_value=file)
    context.user_data = {}
    monkeypatch.setattr(handlers, "_is_owner", lambda update: True)
    monkeypatch.setattr(
        handlers, "transcribe_audio", lambda *args, **kwargs: "List my deadlines"
    )
    chat = AsyncMock()
    monkeypatch.setattr(handlers, "chat_message", chat)

    _run(handlers.media_message(update, context))
    chat.assert_awaited_once_with(update, context, text_override="List my deadlines")


def test_telegram_photo_is_replied_in_text(monkeypatch):
    from bot import handlers

    photo = SimpleNamespace(file_id="photo-large")
    message = MagicMock()
    message.photo = [SimpleNamespace(file_id="small"), photo]
    message.voice = None
    message.audio = None
    message.caption = "Explain this"
    message.reply_text = AsyncMock()
    update = MagicMock(message=message)
    context = MagicMock()
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(b"jpeg"))
    context.bot.get_file = AsyncMock(return_value=file)
    monkeypatch.setattr(handlers, "_is_owner", lambda update: True)
    monkeypatch.setattr(
        handlers, "analyze_image", lambda *args, **kwargs: "A timetable for Monday."
    )
    reply_long = AsyncMock()
    monkeypatch.setattr(handlers, "reply_long", reply_long)

    _run(handlers.media_message(update, context))
    context.bot.get_file.assert_awaited_once_with("photo-large")
    reply_long.assert_awaited_once_with(message, "A timetable for Monday.")
