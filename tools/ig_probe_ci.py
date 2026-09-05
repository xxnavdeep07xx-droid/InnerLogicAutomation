#!/usr/bin/env python3
"""
ig_probe_ci.py - read-only Instagram session probe for GitHub Actions.

Validates that the IG_SESSIONID secret is alive BEFORE burning a Reel
upload on it. Replicates the EXACT login flow of step3_upload.upload_instagram
(same pinned Pixel 4 device fingerprint, same stable UUIDs, same UA) so a
green probe means the daily upload will pass the login stage too.

    python tools/ig_probe_ci.py            # probe (needs IG_SESSIONID env)
    python tools/ig_probe_ci.py --quiet    # exit code only, no secrets printed

Exit codes: 0 = session alive | 1 = config error | 2 = login rejected |
            3 = login OK but session validation failed
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote

# allow `python tools/ig_probe_ci.py` from the repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# MUST mirror step3_upload._IG_DEVICE - same "phone" every run, everywhere.
_IG_DEVICE = {
    "app_version": "269.0.0.18.75",
    "android_version": 30,
    "android_release": "11",
    "dpi": "480dpi",
    "device": "Pixel 4",
    "model": "Pixel 4",
    "cpu": "qcom",
    "version_code": "410532554",
    "manufacturer": "Google",
    "device_manufacturer": "Google",
}


def _ig_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"innerlogic-ig:{name}"))


def build_client():
    from instagrapi import Client

    cl = Client()
    cl.delay_range = [2, 6]
    cl.request_timeout = 90
    cl.set_device(dict(_IG_DEVICE))
    cl.set_locale("en_US")
    cl.set_country("US")
    cl.set_timezone_offset(0)
    cl.set_uuids({
        "phone_id": _ig_uuid("phone"),
        "uuid": _ig_uuid("device"),
        "client_session_id": _ig_uuid("session"),
        "advertising_id": _ig_uuid("ads"),
        "device_id": _ig_uuid("device"),
    })
    cl.set_user_agent(
        "Instagram 269.0.0.18.75 Android (30/11; 480dpi; 440x2400; "
        "Google; Pixel 4; Pixel 4; qcom; en_US; 410532554)")
    return cl


def main() -> int:
    ap = argparse.ArgumentParser(description="probe IG session (read-only)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    session_id = unquote(os.getenv("IG_SESSIONID", "").strip())
    if not session_id:
        print("IG_SESSIONID missing - set the GitHub secret first")
        return 1
    say(f"sessionid present ({len(session_id)} chars, ds_user_id "
        f"{session_id.split(':')[0]})")

    cl = build_client()
    say("logging in with the pinned production device fingerprint ...")
    try:
        ok = cl.login_by_sessionid(session_id)
    except Exception as exc:
        print(f"PROBE FAILED at login_by_sessionid: {str(exc)[:300]}")
        return 2
    say(f"login_by_sessionid -> {ok}")

    try:
        me = cl.account_info()
        say(f"account: {me.username} | media_count: "
            f"{getattr(me, 'media_count', '?')}")
    except Exception as exc:
        print(f"PROBE FAILED at account_info (session not accepted): "
              f"{str(exc)[:300]}")
        return 3

    try:
        cl.get_timeline_feed()
        say("timeline feed OK")
    except Exception as exc:
        print(f"PROBE FAILED at timeline feed: {str(exc)[:300]}")
        return 3

    print("PROBE OK - Instagram session is alive and upload-ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
