#!/usr/bin/env python3
"""
clear_channel.py - delete EVERY video on the InnerLogic YouTube channel.

Built for the "start fresh" request: the channel is fully automated, so old
test videos from earlier pipeline versions are dead weight. This tool runs on
GitHub Actions (workflow_dispatch job) with the SAME YT_* secrets the daily
uploader already uses - nothing is stored locally.

What it does
------------
1. Authenticates with the saved OAuth 2.0 refresh token (CI-safe, no browser).
2. Reads the channel's uploads playlist and lists EVERY video
   (id, title, publish date, duration, view count).
3. If (and only if) CONFIRM_DELETE == "DELETE":
       deletes each video via videos().delete, one by one, and prints a
       running tally. Individual failures never stop the loop.
   otherwise:
       DRY RUN - prints the list plus a giant banner and exits 0.

Safety
------
* Deletion is IRREVERSIBLE (YouTube Data API has no trash can).
* The script refuses to delete unless CONFIRM_DELETE matches "DELETE"
  exactly (all caps), or --yes is passed explicitly on the CLI.
* A --limit N flag caps how many videos a single run may delete (0 = all),
  so a partial cleanup is possible without listing ids by hand.

Usage
-----
    python tools/clear_channel.py                 # list only (dry run)
    CONFIRM_DELETE=DELETE python tools/clear_channel.py
    python tools/clear_channel.py --yes           # same as CONFIRM_DELETE
    python tools/clear_channel.py --limit 5 --yes # delete at most 5
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# allow `python tools/clear_channel.py` from the repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIRM_PHRASE = "DELETE"


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"ERROR: missing environment variable {name} "
                 "(add it under Settings > Secrets and variables > Actions)")
    return value


def build_service():
    """YouTube Data API client from the shared upload secrets."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=_require_env("YT_REFRESH_TOKEN"),
        client_id=_require_env("YT_CLIENT_ID"),
        client_secret=_require_env("YT_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        # NOTE: no 'scopes' argument - same reasoning as step3_upload.py:
        # Google returns the token with its ORIGINAL scope set on refresh.
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def list_all_videos(youtube) -> list[dict]:
    """Every video on the channel, via the uploads playlist."""
    channel = youtube.channels().list(part="contentDetails,snippet",
                                      mine=True).execute()
    items = channel.get("items", [])
    if not items:
        sys.exit("ERROR: no channel found for these credentials")
    ch = items[0]
    uploads_id = (ch.get("contentDetails", {})
                    .get("relatedPlaylists", {}).get("uploads", ""))
    if not uploads_id:
        sys.exit("ERROR: channel has no uploads playlist")

    print(f"  channel : {ch['snippet'].get('title', '?')}")
    print(f"  uploads : {uploads_id}")

    videos: list[dict] = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="contentDetails,status",
            playlistId=uploads_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for it in resp.get("items", []):
            vid = (it.get("contentDetails", {}).get("videoId", ""))
            # privacyStatus of the ITEM, not the video - good enough to show
            status = (it.get("status", {}) or {}).get("privacyStatus", "?")
            videos.append({"id": vid, "list_privacy": status})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if not videos:
        return []

    # hydrate titles/dates/stats in one batched call per 50 ids
    for i in range(0, len(videos), 50):
        batch = videos[i:i + 50]
        resp = youtube.videos().list(
            part="snippet,status,statistics,contentDetails",
            id=",".join(v["id"] for v in batch),
        ).execute()
        by_id = {v["id"]: v for v in resp.get("items", [])}
        for v in batch:
            full = by_id.get(v["id"], {})
            sn = full.get("snippet", {})
            st = full.get("status", {})
            stats = full.get("statistics", {})
            v.update({
                "title": sn.get("title", "(unavailable)"),
                "published": (sn.get("publishedAt", "") or "")[:10],
                "privacy": st.get("privacyStatus", v.get("list_privacy", "?")),
                "views": int(stats.get("viewCount", 0) or 0),
            })
    return videos


def _fmt_duration(iso: str) -> str:
    """PT29S -> 0:29, PT1M3S -> 1:03 (best effort, display only)."""
    import re
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso or "")
    if not m:
        return "-"
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return (f"{h}:{mnt:02d}:{s:02d}" if h else f"{mnt}:{s:02d}")


def delete_videos(youtube, videos: list[dict], limit: int) -> tuple[int, int]:
    """Delete up to `limit` videos; returns (deleted, failed)."""
    deleted = failed = 0
    targets = videos if limit <= 0 else videos[:limit]
    from googleapiclient.errors import HttpError

    for n, v in enumerate(targets, 1):
        vid, title = v["id"], v.get("title", "?")
        try:
            youtube.videos().delete(id=vid).execute()
            deleted += 1
            print(f"  [{n}/{len(targets)}] DELETED {vid}  {title[:60]}")
        except HttpError as exc:
            failed += 1
            reason = ""
            try:
                reason = exc.error_details[0].get("reason", "")
            except Exception:
                pass
            print(f"  [{n}/{len(targets)}] FAILED  {vid}  {title[:50]}  "
                  f"(HTTP {exc.resp.status} {reason})")
        except Exception as exc:
            failed += 1
            print(f"  [{n}/{len(targets)}] FAILED  {vid}  {str(exc)[:80]}")
    return deleted, failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List (and optionally delete) every video on the channel")
    parser.add_argument("--yes", action="store_true",
                        help="skip the CONFIRM_DELETE env check and delete")
    parser.add_argument("--limit", type=int, default=0,
                        help="max videos to delete this run (0 = all)")
    args = parser.parse_args()

    load_env_file()
    confirmed = args.yes or \
        os.getenv("CONFIRM_DELETE", "").strip() == CONFIRM_PHRASE

    print("=" * 62)
    print("  CLEAR CHANNEL - list every video on the InnerLogic channel")
    print("=" * 62)

    youtube = build_service()
    videos = list_all_videos(youtube)

    if not videos:
        print("  channel is ALREADY EMPTY - nothing to do")
        return 0

    print(f"\n  found {len(videos)} video(s):\n")
    for n, v in enumerate(videos, 1):
        print(f"  {n:3d}. {v['id']}  {v.get('published', '?')}  "
              f"{v.get('privacy', '?'):8s}  {v.get('views', 0):>7d} views  "
              f"{v.get('title', '?')[:58]}")
    print()

    if not confirmed:
        print("=" * 62)
        print("  DRY RUN - NOTHING WAS DELETED.")
        print(f"  To delete ALL {len(videos)} video(s) IRREVERSIBLY, run this")
        print(f'  workflow with confirm = "{CONFIRM_PHRASE}" '
              "(or set CONFIRM_DELETE env).")
        print("=" * 62)
        return 0

    print(f"  CONFIRMED - deleting {len(videos) if args.limit <= 0 else min(args.limit, len(videos))}"
          f" video(s){f' (limit {args.limit})' if args.limit > 0 else ''} ...\n")
    deleted, failed = delete_videos(youtube, videos, args.limit)
    print(f"\n  done: {deleted} deleted, {failed} failed "
          f"({datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC)")
    if deleted and not failed:
        print("  channel is now clear - the next daily run starts fresh")
    return 1 if (failed and not deleted) else 0


def load_env_file() -> None:
    """Local runs: read the repo .env (CI passes real env vars instead)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
