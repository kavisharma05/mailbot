"""Build .env.claude_session.accountN from devtools/cookie export JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def _cookie_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept either a devtools export or scripts/_export_claude_cookies_once.py output."""
    if isinstance(data.get("cookies"), list):
        return [c for c in data["cookies"] if isinstance(c, dict)]
    rows: list[dict[str, Any]] = []
    for source in data.get("sources") or []:
        if not isinstance(source, dict):
            continue
        rows.extend(c for c in source.get("cookies") or [] if isinstance(c, dict))
    return rows


def _cookie_header(data: dict[str, Any]) -> str:
    if data.get("cookieHeader"):
        return str(data["cookieHeader"])
    pairs: list[str] = []
    seen: set[str] = set()
    for cookie in _cookie_rows(data):
        name = str(cookie.get("name") or "")
        value = cookie.get("value")
        if not name or value in (None, "") or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_value(data: dict[str, Any], name: str) -> str:
    for cookie in _cookie_rows(data):
        if cookie.get("name") == name and cookie.get("value"):
            return str(cookie["value"])
    return ""


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    export_path = root / ".env.claude_session_export.account1.json"
    if len(sys.argv) > 1:
        export_path = Path(sys.argv[1])
    data = json.loads(export_path.read_text(encoding="utf-8"))
    out = root / ".env.claude_session.account1"
    existing = dotenv_values(out) if out.exists() else {}
    acct = data.get("account") or {}
    org = str(data.get("org_uuid") or existing.get("CLAUDE_ORG_UUID") or "")
    if not org:
        for org_row in data.get("organizations") or []:
            capabilities = org_row.get("capabilities") or []
            if "chat" in capabilities:
                org = str(org_row.get("uuid") or "")
                break
    if not org:
        org = "4ba41588-59a4-4071-ab77-4aa30dbdcc56"

    lines = [
        "# Claude session proxy TEST — account 1 (gitignored)",
        "CLAUDE_SESSION_ENABLED=true",
        "CLAUDE_SESSION_LABEL=account1",
        f"CLAUDE_ORG_UUID={org}",
        f"CLAUDE_ACCOUNT_UUID={acct.get('uuid') or existing.get('CLAUDE_ACCOUNT_UUID', '')}",
        f"CLAUDE_EMAIL={acct.get('email_address') or existing.get('CLAUDE_EMAIL', '')}",
        f"CLAUDE_MODEL={existing.get('CLAUDE_MODEL') or 'claude-sonnet-4-6'}",
        "# Filled by scripts/_export_claude_cookies_once.py + this writer:",
        f"CLAUDE_SESSION_KEY={_cookie_value(data, 'sessionKey')}",
        f"CLAUDE_COOKIE_HEADER={_cookie_header(data)}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
