#!/usr/bin/env python3
"""
yt_purge.py - delete ALL videos on the channel (full wipe).

Built for the "clear the channel and restart" workflow. Lists every video
on the channel's uploads playlist and deletes each one via the YouTube
Data API, printing an audited line per video.

Auth: the same three GitHub Secrets the daily upload uses:
    YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN

IMPORTANT - token scope:
    videos.delete requires the https://www.googleapis.com/auth/youtube.force-ssl
    scope (youtube.upload alone is NOT enough). Regenerate the refresh token
    with the updated tools/yt_refresh_token.py (it now requests upload +
    force-ssl together) and update the YT_REFRESH_TOKEN secret. If the token
    lacks the scope, this tool stops after the FIRST 403 and explains it.

Usage (local):   python tools/yt_purge.py --yes
Usage (CI):      dispatch the "purge_channel" workflow with confirm=PURGE
Destructive:     there is no undo - deleted videos are gone (API deletes do
                 not even go to the trash). The audit trail is the log.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _youtube_service():
    """Same refresh-token auth as step3_upload.upload_youtube."""
    import os
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    client_id = (os.getenv("YT_CLIENT_ID", "") or "").strip()
    client_secret = (os.getenv("YT_CLIENT_SECRET", "") or "").strip()
    refresh_token = (os.getenv("YT_REFRESH_TOKEN", "") or "").strip()
    if not (client_id and client_secret and refresh_token):
        sys.exit("ERROR: YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN "
                 "must all be set")
    creds = Credentials(
        token=None, refresh_token=refresh_token,
        client_id=client_id, client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token")
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds,
                 cache_discovery=False)


def list_all_videos(yt) -> list[dict]:
    """Every video on the channel: uploads playlist -> playlistItems pages."""
    import os
    channel = yt.channels().list(mine=True, part="contentDetails,snippet") \
        .execute()
    items = channel.get("items") or []
    if not items:
        sys.exit("ERROR: no channel found for these credentials")
    ch = items[0]
    uploads_id = (ch.get("contentDetails", {}).get("relatedPlaylists", {})
                  .get("uploads", ""))
    print(f"  channel: {ch.get('snippet', {}).get('title', '?')} "
          f"(uploads playlist {uploads_id})")
    videos: list[dict] = []
    page_token = None
    while True:
        resp = yt.playlistItems().list(
            playlistId=uploads_id, part="snippet,contentDetails",
            maxResults=50, pageToken=page_token).execute()
        for it in resp.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId", "")
            title = it.get("snippet", {}).get("title", "?")
            if vid:
                videos.append({"id": vid, "title": title})
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return videos


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete ALL videos on the channel (DESTRUCTIVE)")
    parser.add_argument("--yes", action="store_true",
                        help="actually delete (without this: dry-run list)")
    parser.add_argument("--keep", default="",
                        help="comma-separated video IDs to keep")
    args = parser.parse_args()

    print("=" * 60)
    print("  YT PURGE - delete all channel videos")
    print(f"  mode: {'DELETE' if args.yes else 'DRY-RUN (list only)'}")
    print("=" * 60)

    yt = _youtube_service()
    videos = list_all_videos(yt)
    keep = {v.strip() for v in args.keep.split(",") if v.strip()}
    targets = [v for v in videos if v["id"] not in keep]
    if keep:
        print(f"  keeping {len(videos) - len(targets)} video(s): {sorted(keep)}")
    if not targets:
        print("  channel is already empty - nothing to delete")
        return 0
    for i, v in enumerate(targets, 1):
        print(f"  [{i:>3}/{len(targets)}] {v['id']}  {v['title'][:60]}")
    if not args.yes:
        print(f"\n  DRY-RUN: {len(targets)} video(s) would be deleted. "
              "Pass --yes to delete.")
        return 0

    print()
    deleted, failed = 0, 0
    for i, v in enumerate(targets, 1):
        try:
            yt.videos().delete(id=v["id"]).execute()
            deleted += 1
            print(f"  [{i:>3}/{len(targets)}] DELETED {v['id']}  "
                  f"{v['title'][:52]}")
        except Exception as exc:
            msg = str(exc)
            failed += 1
            print(f"  [{i:>3}/{len(targets)}] FAILED  {v['id']}  "
                  f"{msg[:100]}")
            if "insufficientPermissions" in msg or "Forbidden" in msg:
                print()
                print("  BLOCKED: the refresh token lacks the youtube.force-ssl")
                print("  scope (delete needs more than upload). Fix:")
                print("    1. python tools/yt_refresh_token.py   (updated - now")
                print("       requests upload + force-ssl together)")
                print("    2. update the YT_REFRESH_TOKEN GitHub secret")
                print("    3. re-run this purge")
                break
    print("=" * 60)
    print(f"  done: {deleted} deleted, {failed} failed, "
          f"{len(targets) - deleted - failed} skipped")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
