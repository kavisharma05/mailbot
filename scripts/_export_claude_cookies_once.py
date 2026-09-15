"""One-off: read claude.ai cookies from Cursor browser partition (local only)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

try:
    import win32crypt  # type: ignore
except ImportError:
    win32crypt = None


def decrypt_chromium(value: bytes) -> str | None:
    if not value:
        return None
    if value[:3] == b"v10" or value[:3] == b"v11":
        if win32crypt is None:
            return None
        try:
            return win32crypt.CryptUnprotectData(value[3:], None, None, None, 0)[1].decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return None
    try:
        return value.decode("utf-8", errors="replace")
    except Exception:
        return None


def main() -> None:
    appdata = os.environ.get("APPDATA", "")
    paths = glob.glob(os.path.join(appdata, "Cursor", "Partitions", "cursor-browser*", "Network", "Cookies"))
    paths.append(os.path.join(appdata, "Cursor", "Network", "Cookies"))

    all_cookies: list[dict] = []
    for db_path in paths:
        entry: dict = {"db": db_path}
        try:
            con = None
            tmp_path = None
            try:
                uri = "file:" + db_path.replace("\\", "/") + "?mode=ro&immutable=1"
                con = sqlite3.connect(uri, uri=True)
            except Exception:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".cookies") as tmp:
                    tmp_path = tmp.name
                shutil.copy2(db_path, tmp_path)
                con = sqlite3.connect(tmp_path)
            cur = con.cursor()
            cur.execute(
                """
                SELECT host_key, name, value, encrypted_value, path, is_httponly, is_secure, expires_utc
                FROM cookies
                WHERE host_key LIKE '%claude%'
                """
            )
            cookies = []
            for host, name, value, enc, path, httponly, secure, expires in cur.fetchall():
                plain = value if value else decrypt_chromium(enc)
                cookies.append(
                    {
                        "host": host,
                        "name": name,
                        "value": plain,
                        "path": path,
                        "httponly": bool(httponly),
                        "secure": bool(secure),
                        "expires_utc": expires,
                    }
                )
            con.close()
            if tmp_path:
                os.unlink(tmp_path)
            entry["cookies"] = cookies
        except Exception as exc:
            entry["error"] = str(exc)
        all_cookies.append(entry)

    out = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "win32crypt_available": win32crypt is not None,
        "sources": all_cookies,
    }
    out_path = os.path.join(os.path.dirname(__file__), "..", ".env.claude_session_export.account1.json")
    out_path = os.path.normpath(out_path)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(out_path)
    for src in all_cookies:
        names = [c["name"] for c in src.get("cookies", [])]
        if names:
            print(src["db"], "->", ", ".join(sorted(set(names))))


if __name__ == "__main__":
    main()
