#!/usr/bin/env python3
"""Subscribe app to WABA webhooks and send a test template message."""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from config import get_settings


def _require(value: str, name: str) -> str:
    if not value or not str(value).strip():
        print(f"Missing {name}. Set it in .env", file=sys.stderr)
        sys.exit(1)
    return str(value).strip()


def send_test_template(*, to: str, template: str, token: str, phone_id: str, version: str) -> None:
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {"name": template, "language": {"code": "en_US"}},
    }
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    print(resp.status_code, resp.text)


def subscribe_waba(*, waba_id: str, token: str, version: str) -> None:
    url = f"https://graph.facebook.com/{version}/{waba_id}/subscribed_apps"
    resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    print("subscribed_apps:", resp.status_code, resp.text)


def main() -> None:
    parser = argparse.ArgumentParser(description="WhatsApp Cloud API setup helpers")
    parser.add_argument("--subscribe", action="store_true", help="POST WABA subscribed_apps")
    parser.add_argument("--test-template", action="store_true", help="Send hello_world / configured template")
    parser.add_argument("--template", default="", help="Template name override")
    parser.add_argument("--to", default="", help="Recipient E.164 without + (defaults to WHATSAPP_OWNER_PHONE)")
    args = parser.parse_args()

    settings = get_settings()
    token = _require(settings.whatsapp_access_token, "WHATSAPP_ACCESS_TOKEN")
    phone_id = _require(settings.whatsapp_phone_number_id, "WHATSAPP_PHONE_NUMBER_ID")
    version = (settings.whatsapp_api_version or "v22.0").strip()

    if args.subscribe:
        waba = _require(settings.whatsapp_waba_id or os.getenv("WHATSAPP_WABA_ID", ""), "WHATSAPP_WABA_ID")
        subscribe_waba(waba_id=waba, token=token, version=version)

    if args.test_template:
        to = args.to or _require(settings.whatsapp_owner_phone, "WHATSAPP_OWNER_PHONE")
        template = args.template or (settings.whatsapp_alert_template or "hello_world")
        send_test_template(to=to, template=template, token=token, phone_id=phone_id, version=version)

    if not args.subscribe and not args.test_template:
        parser.print_help()


if __name__ == "__main__":
    main()
