"""Persist Claude devtools-style export from Cursor browser tab via CDP (local test)."""
from __future__ import annotations

import json
import os
import sys

# Run from repo root; output is gitignored via .env.* pattern.
OUT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".env.claude_session_export.account1.json"))

JS = r"""
(async () => {
  const all = await cookieStore.getAll();
  const cookieHeader = all.map(c => c.name + '=' + c.value).join('; ');
  const ls = Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]));
  const ss = Object.fromEntries(Object.keys(sessionStorage).map(k => [k, sessionStorage.getItem(k)]));
  let account = null;
  let organizations = null;
  try {
    account = await (await fetch('/api/account', { credentials: 'include' })).json();
  } catch (e) {}
  try {
    organizations = await (await fetch('/api/organizations', { credentials: 'include' })).json();
  } catch (e) {}
  return JSON.stringify({
    exportedAt: new Date().toISOString(),
    pageUrl: location.href,
    documentCookie: document.cookie,
    cookies: all,
    cookieHeader,
    localStorage: ls,
    sessionStorage: ss,
    account,
    organizations,
    notes: {
      httponlyCookiesNotVisibleToJs: ['sessionKey', '__Secure-next-auth.session-token'],
    },
  });
})()
"""


def main() -> None:
    print(
        "This script expects JSON on stdin (devtools dump). "
        "Pipe browser evaluate output or use MCP export.",
        file=sys.stderr,
    )
    data = json.load(sys.stdin)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(OUT)


if __name__ == "__main__":
    main()
