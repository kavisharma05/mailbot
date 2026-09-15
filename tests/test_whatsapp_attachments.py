"""WhatsApp document upload regressions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from config import Settings
from services.messaging.whatsapp import WhatsAppClient


def _settings() -> Settings:
    return Settings(
        telegram_bot_token="t",
        telegram_owner_chat_id="1",
        telegram_webhook_secret="s",
        groq_api_key="g",
        gmail_client_id="c",
        gmail_client_secret="cs",
        gmail_refresh_token="r",
        gmail_user="u@example.com",
        supabase_url="https://example.supabase.co",
        supabase_service_key="sk",
        task_secret="ts",
        whatsapp_enabled=True,
        whatsapp_access_token="tok",
        whatsapp_phone_number_id="123",
        whatsapp_owner_phone="1111111111",
    )


def test_send_document_uploads_with_real_mime_type():
    client = WhatsAppClient(_settings())
    upload_resp = MagicMock(status_code=200)
    upload_resp.json.return_value = {"id": "media-1"}
    send_resp = MagicMock(status_code=200)
    fake_http = MagicMock()
    fake_http.post = AsyncMock(side_effect=[upload_resp, send_resp])

    async def _run():
        with patch("services.messaging.whatsapp._http", return_value=fake_http):
            return await client.send_document(
                "1111111111",
                b"%PDF-1.4\n",
                "case.pdf",
                caption="Reading",
                mime_type="application/pdf",
            )

    assert asyncio.run(_run()) is True
    upload_call = fake_http.post.call_args_list[0]
    assert upload_call.kwargs["data"]["type"] == "application/pdf"
    assert upload_call.kwargs["files"]["file"][2] == "application/pdf"
    send_call = fake_http.post.call_args_list[1]
    assert send_call.kwargs["json"]["document"]["id"] == "media-1"


def test_send_document_guesses_mime_from_filename():
    client = WhatsAppClient(_settings())
    upload_resp = MagicMock(status_code=200)
    upload_resp.json.return_value = {"id": "media-2"}
    send_resp = MagicMock(status_code=200)
    fake_http = MagicMock()
    fake_http.post = AsyncMock(side_effect=[upload_resp, send_resp])

    async def _run():
        with patch("services.messaging.whatsapp._http", return_value=fake_http):
            return await client.send_document("1111111111", b"hello", "notes.txt")

    assert asyncio.run(_run()) is True
    upload_call = fake_http.post.call_args_list[0]
    assert upload_call.kwargs["data"]["type"] == "text/plain"
