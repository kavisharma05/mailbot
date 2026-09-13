import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from config import get_settings


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    get_settings.cache_clear()
    from app import fastapi_app

    return TestClient(fastapi_app)


def test_whatsapp_verify_challenge(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    get_settings.cache_clear()
    resp = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "12345"},
    )
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_whatsapp_verify_rejects_bad_token(client):
    resp = client.get(
        "/whatsapp/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert resp.status_code == 403


def test_whatsapp_post_signature(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    get_settings.cache_clear()
    body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode("utf-8")
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    resp = client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert resp.status_code == 200


def test_whatsapp_post_bad_signature(client):
    body = b"{}"
    resp = client.post("/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=dead"})
    assert resp.status_code == 403


def test_extract_inbound_skips_statuses():
    from services.messaging.webhook import extract_inbound_messages

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {"value": {"statuses": [{"id": "wamid"}]}},
                    {
                        "value": {
                            "messages": [
                                {"from": "917489491188", "id": "m1", "type": "text", "text": {"body": "hi"}}
                            ]
                        }
                    },
                ]
            }
        ],
    }
    msgs = extract_inbound_messages(payload)
    assert len(msgs) == 1
    assert msgs[0][0] == "917489491188"
