#!/usr/bin/env python3
"""
yt_refresh_token.py - ONE-TIME helper: create a YouTube API refresh token.

GitHub Actions cannot show you a login page, so the upload code authenticates
with a long-lived OAuth 2.0 REFRESH TOKEN instead. You create that token once
on any computer with a browser, paste it into GitHub Secrets, and every daily
run refreshes it automatically - forever (unless you revoke it).

One-time setup
--------------
1. https://console.cloud.google.com  -> create a project (any name)
2. APIs & Services -> Library -> search "YouTube Data API v3" -> ENABLE
3. APIs & Services -> OAuth consent screen:
      - User type: External  ->  Create
      - App name: anything; add your Google account under "Test users"
      - (leave everything else default; no verification needed for testing)
4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
      - Application type: **Desktop app**  ->  Create
      - copy the Client ID and Client Secret
5. Run this helper from the repo root:
      pip install google-auth-oauthlib
      python tools/yt_refresh_token.py --client-id "XXXX" --client-secret "YYYY"
6. A browser window opens. Sign in with the Google account that owns your
   YouTube channel. If Google warns "app isn't verified": click
   "Advanced" -> "Go to <app> (unsafe)" - it is YOUR own app.
7. The refresh token is printed. Put these three values in GitHub Secrets:
      YT_CLIENT_ID     = the client id
      YT_CLIENT_SECRET = the client secret
      YT_REFRESH_TOKEN = the printed refresh token

Notes
-----
- The refresh token keeps working until you revoke it (Google Account ->
  Security -> Third-party access). A daily upload counts as usage, which
  keeps it alive.
- Never commit the token anywhere; only GitHub Secrets.
"""

from __future__ import annotations

import argparse
import sys

# youtube.upload        -> videos.insert (uploads) + thumbnails.set
# youtube.force-ssl     -> videos.delete / videos.update (purge, privacy)
# Requested TOGETHER so one refresh token covers the whole automation;
# Google shows both on the consent screen.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_CONFIG_TEMPLATE = {
    "installed": {
        "client_id": "",
        "client_secret": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"],
    }
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time YouTube OAuth refresh-token generator",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", help="OAuth 'Desktop app' client ID")
    parser.add_argument("--client-secret", help="OAuth 'Desktop app' client secret")
    args = parser.parse_args()

    client_id = args.client_id or input("Client ID: ").strip()
    client_secret = args.client_secret or input("Client secret: ").strip()
    if not client_id or not client_secret:
        sys.exit("ERROR: client id and secret are required")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install google-auth-oauthlib")

    config = dict(CLIENT_CONFIG_TEMPLATE)
    config["installed"] = dict(CLIENT_CONFIG_TEMPLATE["installed"],
                               client_id=client_id, client_secret=client_secret)
    flow = InstalledAppFlow.from_client_config(config, SCOPES)

    print("\nA browser window will open - sign in with the channel's Google "
          "account and approve BOTH permissions (upload + manage/delete)...\n")
    creds = flow.run_local_server(port=0, prompt="consent",
                                  access_type="offline")

    print("=" * 60)
    print("  SUCCESS - add these to GitHub Secrets")
    print("=" * 60)
    print(f"\n  YT_CLIENT_ID:\n    {client_id}\n")
    print(f"  YT_CLIENT_SECRET:\n    {client_secret}\n")
    print(f"  YT_REFRESH_TOKEN:\n    {creds.refresh_token}\n")
    print("=" * 60)
    print("  The daily workflow will refresh this token automatically.")
    print("  This token can now ALSO delete/update videos (force-ssl scope)")
    print("  - e.g. the Purge Channel workflow or tools/yt_purge.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
