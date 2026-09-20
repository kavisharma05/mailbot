"""MapMyAccess portal client (pure HTTP — no browser needed).

Login flow (reverse-engineered from the portal's own js/app.js):
- The portal captcha is generated and validated client-side only, so the JSON
  auth API can be called directly.
- POST /mma-auth/MapMyAccess-login?redirect=false with {username, password,
  Type: "MapMyAccess"} returns {success, msg} and sets a 24h JWT cookie
  "MapMyAccess-auth-token".
- If the account is ever switched to the OTP flow, we request the code via
  /mma-auth/MapMyAccess-getotplogin and read it from the linked Gmail inbox
  (see otp.py), then retry the login with the OTP field set.

The JWT is cached in Supabase (imt_state) so restarts and multiple daily runs
reuse it until it nears expiry.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from imt_access import store
from imt_access.settings import IMTSettings, get_imt_settings

# Local session cache used when the imt_state table is not available yet
# (migration pending) — avoids a fresh login on every run. Gitignored.
_SESSION_FILE = Path(__file__).resolve().parent.parent / "data" / "imt_session.json"

logger = logging.getLogger(__name__)

_AUTH_COOKIE = "MapMyAccess-auth-token"
_USER_COOKIE = "MapMyAccess-user"
_LOGIN_PATH = "/mma-auth/MapMyAccess-login?redirect=false"
_GETOTP_PATH = "/mma-auth/MapMyAccess-getotplogin?redirect=false"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class PortalError(Exception):
    """Portal login or fetch failed."""


def _jwt_expiry(token: str) -> float | None:
    """Extract exp (unix seconds) from a JWT without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


class PortalClient:
    def __init__(self, settings: IMTSettings | None = None) -> None:
        self.settings = settings or get_imt_settings()
        self.base_url = self.settings.mapmyaccess_base_url
        self._client = httpx.Client(
            timeout=httpx.Timeout(45.0),
            follow_redirects=True,
            headers={"User-Agent": _UA},
        )
        self.display_name: str | None = None

    # -- context manager sugar so callers can use `with PortalClient() as p` --
    def __enter__(self) -> "PortalClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ auth

    def _apply_token(self, token: str) -> None:
        self._client.cookies.set(_AUTH_COOKIE, token, domain=self._cookie_domain())

    def _cookie_domain(self) -> str:
        return self.base_url.split("://", 1)[-1].split("/")[0]

    def _read_session_token(self) -> str | None:
        token = store.get_session_token()
        if token:
            return token
        try:
            if _SESSION_FILE.exists():
                data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
                saved = float(data.get("saved_at") or 0)
                # Local cache is only trusted for 20h (token lifetime is 24h).
                if time.time() - saved < 20 * 3600:
                    return str(data.get("token") or "") or None
        except Exception:
            pass
        return None

    @staticmethod
    def _write_session_file(token: str, user_display: str | None) -> None:
        try:
            _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SESSION_FILE.write_text(
                json.dumps(
                    {"token": token, "user": user_display, "saved_at": time.time()}
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Local session cache write failed: %s", exc)

    def _restore_cached_session(self) -> bool:
        token = self._read_session_token()
        if not token:
            return False
        exp = _jwt_expiry(token)
        margin = self.settings.imt_session_refresh_margin
        if exp is not None and exp < time.time() + margin:
            logger.info("Cached MapMyAccess session expires soon — will re-login")
            return False
        self._apply_token(token)
        return True

    def ensure_session(self, *, force_login: bool = False) -> None:
        """Make sure we hold a valid auth cookie; login if needed."""
        if not self.settings.configured:
            raise PortalError(
                "MAPMYACCESS_EMAIL / MAPMYACCESS_PASSWORD are not configured"
            )
        if not force_login and self._restore_cached_session():
            if self._session_looks_alive():
                return
        self.login()

    def _session_looks_alive(self) -> bool:
        """Cheap liveness probe: authenticated /resources has no login modal."""
        try:
            resp = self._client.get(f"{self.base_url}/resources")
        except Exception as exc:
            logger.warning("Session probe failed: %s", exc)
            return False
        if resp.status_code != 200:
            return False
        return "MapMyAccessLoginButtonPopup" not in resp.text

    def login(self) -> None:
        """Full login: warm-up GET, direct login, OTP fallback via Gmail."""
        logger.info("MapMyAccess login as %s", self.settings.mapmyaccess_email)
        self._client.cookies.clear()
        # Warm-up: establishes any server-side session cookies.
        self._client.get(self.base_url)

        body = {
            "username": self.settings.mapmyaccess_email,
            "password": self.settings.mapmyaccess_password,
            "Type": "MapMyAccess",
        }
        resp = self._client.post(f"{self.base_url}{_LOGIN_PATH}", json=body)
        data = self._parse_auth_response(resp)

        if data.get("success"):
            self._finish_login(data)
            return

        msg = str(data.get("msg") or "")
        if self._looks_like_otp_required(msg, resp):
            logger.info("Portal asked for OTP — requesting code and polling Gmail")
            self._login_with_otp(body)
            return

        raise PortalError(f"MapMyAccess login failed: {msg or resp.text[:200]}")

    @staticmethod
    def _looks_like_otp_required(msg: str, resp: httpx.Response) -> bool:
        text = msg.lower()
        return "otp" in text or "verification code" in text or "authentication code" in text

    def _login_with_otp(self, body: dict[str, Any]) -> None:
        from imt_access.otp import fetch_otp_from_gmail

        requested_at = time.time()
        otp_resp = self._client.post(f"{self.base_url}{_GETOTP_PATH}", json=body)
        try:
            otp_data = otp_resp.json()
        except Exception:
            otp_data = {}
        if otp_resp.status_code != 200 or not otp_data.get("success"):
            raise PortalError(
                f"MapMyAccess OTP request failed: {otp_data.get('msg') or otp_resp.text[:200]}"
            )

        code = fetch_otp_from_gmail(not_before=requested_at - 60)
        if not code:
            raise PortalError("OTP was emailed but could not be read from Gmail")

        attempt = dict(body)
        attempt["OTP"] = code
        resp = self._client.post(f"{self.base_url}{_LOGIN_PATH}", json=attempt)
        data = self._parse_auth_response(resp)
        if data.get("success"):
            self._finish_login(data)
            return
        raise PortalError(f"MapMyAccess OTP verify failed: {data.get('msg') or resp.text[:200]}")

    @staticmethod
    def _parse_auth_response(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code != 200:
            raise PortalError(f"Auth endpoint returned {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except Exception as exc:
            raise PortalError(f"Auth endpoint returned non-JSON: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            raise PortalError("Auth endpoint returned unexpected payload")
        return data

    def _finish_login(self, data: dict[str, Any]) -> None:
        token = self._client.cookies.get(_AUTH_COOKIE) or data.get("token")
        if not token:
            raise PortalError("Login succeeded but no auth token was returned")
        self._apply_token(str(token))
        user = data.get("user") or {}
        self.display_name = user.get("DisplayName") if isinstance(user, dict) else None
        store.save_session_token(str(token), self.display_name)
        self._write_session_file(str(token), self.display_name)
        logger.info("MapMyAccess login ok (%s)", self.display_name or "session cached")

    # ----------------------------------------------------------------- fetch

    def get(self, path_or_url: str, *, retry_auth: bool = True) -> httpx.Response:
        """GET a portal path or absolute proxy URL, re-logging in once on expiry."""
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self.base_url}/{path_or_url.lstrip('/')}"
        )
        resp = self._client.get(url)
        if retry_auth and self._looks_logged_out(resp):
            logger.info("Session expired mid-run — re-logging in")
            self.login()
            resp = self._client.get(url)
        return resp

    @staticmethod
    def _looks_logged_out(resp: httpx.Response) -> bool:
        if resp.status_code in (401, 403):
            return True
        # A dead upstream proxy session surfaces as a tiny bare Express-style
        # 500 page ("Internal Server Error") while our JWT is still valid.
        if resp.status_code == 500 and len(resp.content) < 2000:
            return True
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            head = resp.text[:60000]
            # The login modal only renders for anonymous users.
            if "MapMyAccessLoginButtonPopup" in head and "favouriteIcon" not in head:
                return True
        return False

    def get_resources_html(self) -> str:
        resp = self.get("/resources")
        if resp.status_code != 200:
            raise PortalError(f"/resources returned {resp.status_code}")
        return resp.text

    def _clear_upstream_proxy_cookies(self) -> None:
        """Drop upstream-site session cookies (SSESS*, ipLogin*) set via the proxy.

        EPW's IP-login module only authenticates the upstream session when no
        session cookie is present. The anonymous SSESS cookie it sets on first
        contact otherwise sticks in our jar and strips subscriber-only content
        (PDF download links) from every subsequent page. Clearing it forces the
        proxy to perform a fresh upstream IP-login on each request.
        """
        jar = self._client.cookies.jar
        doomed = [
            c
            for c in jar
            if c.name.startswith("SSESS") or c.name.lower().startswith("iplogin")
        ]
        for c in doomed:
            try:
                jar.clear(c.domain, c.path, c.name)
            except Exception:
                pass

    def epw_get(self, path: str) -> httpx.Response:
        """GET an EPW page through the portal's authenticated proxy.

        The proxy's upstream EPW session can die while our JWT is still valid,
        surfacing as a 404 or bare 500. Re-login once to rebuild it, and retry
        once more after a short pause for cold upstream caches.
        """
        url = f"https://www-epw-in.imtnagpur.mapmyaccess.com/{path.lstrip('/')}"
        self._clear_upstream_proxy_cookies()
        resp = self.get(url)
        if resp.status_code in (404, 500):
            logger.info(
                "EPW proxy returned %s for %s — refreshing session", resp.status_code, path
            )
            self.login()
            time.sleep(1.0)
            self._clear_upstream_proxy_cookies()
            resp = self.get(url)
        if resp.status_code == 404:
            time.sleep(1.5)
            self._clear_upstream_proxy_cookies()
            resp = self.get(url)
        return resp

    def epw_download_pdf(self, path: str, *, max_bytes: int = 15 * 1024 * 1024) -> bytes:
        resp = self.epw_get(path)
        if resp.status_code != 200:
            raise PortalError(f"EPW PDF {path} returned {resp.status_code}")
        content = resp.content
        if len(content) > max_bytes:
            raise PortalError(f"EPW PDF {path} too large ({len(content)} bytes)")
        if not content.startswith(b"%PDF"):
            raise PortalError(f"EPW PDF {path} did not return a PDF document")
        return content


def health_check() -> dict[str, Any]:
    """Config + login + catalog reachability, for CLI `health` and /healthz."""
    settings = get_imt_settings()
    result: dict[str, Any] = {
        "configured": settings.configured,
        "base_url": settings.mapmyaccess_base_url,
        "daily_enabled": settings.imt_daily_enabled,
    }
    if not settings.configured:
        result["ok"] = False
        result["error"] = "credentials not configured"
        return result
    try:
        with PortalClient(settings) as portal:
            portal.ensure_session()
            html = portal.get_resources_html()
        result["ok"] = True
        result["resources_page_bytes"] = len(html)
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result
