"""One-time script to obtain a Gmail OAuth refresh token.

Run locally after setting GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env.

Examples:
  python scripts/get_gmail_token.py --list-profiles
  python scripts/get_gmail_token.py --profile "Profile 1"
  python scripts/get_gmail_token.py --no-browser   # copy URL into your browser manually
"""

import argparse
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gmail_api.auth import GMAIL_SCOPES

load_dotenv()

CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


def find_chrome() -> Path | None:
    for path in CHROME_PATHS:
        if path.exists():
            return path
    return None


def chrome_user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"


def list_chrome_profiles() -> list[tuple[str, str]]:
    """Return (folder_name, display_name) for each Chrome profile."""
    data_dir = chrome_user_data_dir()
    if not data_dir.exists():
        return []

    profiles: list[tuple[str, str]] = []
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        prefs = entry / "Preferences"
        if not prefs.exists():
            continue

        folder = entry.name
        if folder not in {"Default", "Guest Profile", "System Profile"} and not folder.startswith("Profile "):
            continue

        display = folder
        try:
            import json

            data = json.loads(prefs.read_text(encoding="utf-8"))
            name = data.get("profile", {}).get("name")
            if name:
                display = f'{folder} — "{name}"'
        except Exception:
            pass

        profiles.append((folder, display))

    return profiles


def make_chrome_opener(profile_dir: str):
    chrome = find_chrome()

    def open_in_chrome(url: str, new: int = 0, autoraise: bool = True) -> bool:
        if chrome:
            subprocess.Popen(
                [str(chrome), f'--profile-directory={profile_dir}', url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        webbrowser.open(url, new=new, autoraise=autoraise)
        return True

    return open_in_chrome


def main() -> None:
    parser = argparse.ArgumentParser(description="Obtain a Gmail OAuth refresh token")
    parser.add_argument(
        "--profile",
        help='Chrome profile folder name, e.g. "Default" or "Profile 1" (or set CHROME_PROFILE in .env)',
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List installed Chrome profiles and exit",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the auth URL instead of opening a browser automatically",
    )
    args = parser.parse_args()

    if args.list_profiles:
        profiles = list_chrome_profiles()
        if not profiles:
            print("No Chrome profiles found.")
            sys.exit(1)
        print("Chrome profiles on this machine:\n")
        for folder, display in profiles:
            print(f"  {display}")
        print('\nRun with: python scripts/get_gmail_token.py --profile "Profile 1"')
        return

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
    profile = args.profile or os.getenv("CHROME_PROFILE")

    if args.no_browser:
        flow.redirect_uri = "http://localhost:0/"
        auth_url, _ = flow.authorization_url(prompt="consent")
        print("Open this URL in the Chrome profile you want:\n")
        print(auth_url)
        print("\nAfter approving, paste the full redirect URL here:")
        redirect_response = input().strip()
        flow.fetch_token(authorization_response=redirect_response)
        creds = flow.credentials
    elif profile:
        print(f'Opening Chrome profile: {profile}')
        creds = flow.run_local_server(port=0, open_browser=make_chrome_opener(profile))
    else:
        print("Tip: use --list-profiles to pick a Chrome profile, or --no-browser to open the URL yourself.")
        creds = flow.run_local_server(port=0)

    print("\n--- Add this to your .env ---")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("GMAIL_USER=your@gmail.com  # set to the Gmail account you authorized")
    print("---")


if __name__ == "__main__":
    main()
