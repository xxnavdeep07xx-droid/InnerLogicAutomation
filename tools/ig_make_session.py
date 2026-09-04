#!/usr/bin/env python3
"""
ig_make_session.py - ONE-TIME helper: create a reusable Instagram session.

Why: Instagram blocks/challenges logins from datacenter IPs (GitHub Actions
runners get error 467 "checkpoint required"). A session created ONCE from
your own home IP (trusted) can then be REUSED from CI indefinitely - no
proxy, no paid service.

Run this on YOUR computer (not CI):

    pip install instagrapi
    python tools/ig_make_session.py            # interactive

Two modes
---------
1. username + password  (best): performs a real login from your IP and
   captures the complete device fingerprint + session.
2. sessionid cookie     (fallback): you copy the "sessionid" cookie value
   from instagram.com in your browser DevTools (Application > Cookies),
   the tool validates it and captures the session around it.

The tool prints a JSON blob. Copy it into GitHub:
    Settings > Secrets and variables > Actions > New repository secret
    Name:  IG_SESSION_JSON     Value: <the whole JSON>

The daily workflow then loads this session instead of logging in with a
password - no checkpoint, no 467, fully automated.
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create an IG_SESSION_JSON secret value (run at home)")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--cookie", default="",
                        help="browser 'sessionid' cookie value instead of "
                             "username/password")
    parser.add_argument("--out", default="",
                        help="also write the JSON to this file")
    args = parser.parse_args()

    try:
        from instagrapi import Client
    except ImportError:
        sys.exit("Missing dependency. Run:  pip install instagrapi")

    client = Client()
    client.delay_range = [1, 3]

    if args.cookie:
        print("Logging in with the provided sessionid cookie...")
        client.login_by_sessionid(args.cookie.strip())
    else:
        username = args.username or input("Instagram username: ").strip()
        password = args.password or input("Instagram password: ").strip()
        if not (username and password):
            sys.exit("ERROR: username and password are required")
        print(f"Logging in as {username} (from YOUR ip - trusted)...")
        client.login(username, password)

    username_ok = getattr(client, "username", None)
    print(f"Login OK as: {username_ok or '?'}")

    settings = client.get_settings()
    blob = json.dumps(settings, ensure_ascii=False)
    print()
    print("=" * 60)
    print("  SUCCESS - copy the JSON below into GitHub Secrets as")
    print("  IG_SESSION_JSON  (Settings > Secrets > Actions)")
    print("=" * 60)
    print(blob)
    print("=" * 60)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob)
        print(f"  also saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
