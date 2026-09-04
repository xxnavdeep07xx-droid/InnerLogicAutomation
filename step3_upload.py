#!/usr/bin/env python3
"""
step3_upload.py - Step 3: publish the rendered short to YouTube + Instagram.

What it does
------------
1. Finds the newest run folder containing final_short.mp4 (+ script.txt).
2. Generates upload metadata with the Gemini API from the video's script:
       - a short, hooky title
       - 3-5 niche hashtags (philosophy / dark psychology)
   Falls back to a sane default if Gemini is unavailable. The metadata is
   saved to metadata.json inside the run folder and applied to BOTH uploads.
3. YouTube Shorts (google-api-python-client, YouTube Data API v3):
       - fully unattended: authenticates with a saved OAuth 2.0 REFRESH
         TOKEN from environment/secrets - no browser prompt, CI-safe
       - resumable chunked upload, then prints the watch URL
4. Instagram Reels (instagrapi):
       - logs in with IG_SESSIONID (recommended, most stable) or
         IG_USERNAME + IG_PASSWORD from environment/secrets
       - uploads the video as a Reel, then prints the reel URL

Designed to run unattended on GitHub Actions: missing credentials cause a
platform to be SKIPPED with a clear note (exit 0), never a crashed pipeline.

Secrets / environment variables
-------------------------------
    GEMINI_API_KEY      script->metadata generation (already used by step 1)
    YT_CLIENT_ID        } from Google Cloud OAuth "Desktop app" client
    YT_CLIENT_SECRET    } see tools/yt_refresh_token.py for the one-time
    YT_REFRESH_TOKEN    } browser flow that produces the refresh token
    YT_PRIVACY          optional: public | unlisted | private (default public)
    IG_USERNAME         Instagram username (if not using session id)
    IG_PASSWORD         Instagram password
    IG_SESSIONID        optional but RECOMMENDED: your ig_did session cookie,
                        survives Instagram IP challenges much better on CI

Usage
-----
    python step3_upload.py                  # upload newest run to both
    python step3_upload.py --run-id run_... # specific run
    python step3_upload.py --video path.mp4 # explicit video
    python step3_upload.py --dry-run        # metadata only, no uploads
    python step3_upload.py --skip-instagram # YouTube only
    python step3_upload.py --skip-youtube   # Instagram only

Thumbnail (from step_thumbnail.py's manifest, auto-attached)
    YouTube : 1080x1920 JPG (Shorts-native 9:16) via thumbnails.set right
              after the upload. A 403 here almost always means the channel
              is not enabled for custom thumbnails yet - the log prints
              exactly what to do. (The legacy 1280x720 render is still
              generated as thumbnails/<variant>_youtube_wide.jpg.)
    Instagram: 1080x1920 cover via clip_upload(thumbnail=...).
    AUTO_UPLOAD_THUMBNAIL=false (or --no-thumbnail) skips attaching, so
    variants can be reviewed in output/<run>/thumbnails/ first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from step1_generate import (  # shared config helpers (env loading, model chain)
    is_permanent_model_error,
    load_environment,
    resolve_models,
)

YT_CATEGORY_ID = "27"        # Education
YT_TITLE_MAX = 95            # hard limit is 100; leave headroom for #Shorts
YT_TAGS_MAX_CHARS = 450
IG_SESSION_FILE = ROOT / "output" / "_ig_session.json"

DEFAULT_HASHTAGS = ["#philosophy", "#darkpsychology", "#stoicism",
                    "#psychology", "#selfimprovement"]

# ---------------------------------------------------------------------------
# Run folder / script discovery
# ---------------------------------------------------------------------------

def find_run(output_dir: str, run_id: str | None, video_override: str | None):
    """Return (video_path, run_folder, script_text)."""
    if video_override:
        video = Path(video_override)
        if not video.is_file():
            sys.exit(f"ERROR: video not found: {video}")
        run_folder = video.parent
    else:
        base = Path(output_dir)
        if not base.is_dir():
            sys.exit(f"ERROR: no '{base}' directory - render a video first (main.py)")
        if run_id:
            run_folder = base / run_id
            if not run_folder.is_dir():
                sys.exit(f"ERROR: run folder not found: {run_folder}")
        else:
            candidates = [d for d in base.iterdir()
                          if d.is_dir() and (d / "final_short.mp4").is_file()]
            if not candidates:
                sys.exit("ERROR: no run folder containing final_short.mp4 - "
                         "render a video first (main.py)")
            run_folder = max(candidates, key=lambda d: d.stat().st_mtime)
        video = run_folder / "final_short.mp4"
        if not video.is_file():
            sys.exit(f"ERROR: final_short.mp4 missing in {run_folder}")

    script_text = ""
    script_file = run_folder / "script.txt"
    if script_file.is_file():
        script_text = script_file.read_text(encoding="utf-8").strip()
    else:
        timings_file = run_folder / "word_timings.json"
        if timings_file.is_file():
            try:
                script_text = json.loads(
                    timings_file.read_text(encoding="utf-8")).get("script", "")
            except Exception:
                pass
    return video, run_folder, script_text


# ---------------------------------------------------------------------------
# Metadata generation (Gemini, with defensive fallback)
# ---------------------------------------------------------------------------

def _clean_hashtags(raw: list) -> list[str]:
    tags = []
    for tag in raw:
        text = str(tag).strip().replace(" ", "")
        if not text:
            continue
        if not text.startswith("#"):
            text = "#" + text
        if re.fullmatch(r"#[\w]", text) or len(text) > 30:
            continue
        if text.lower() not in [t.lower() for t in tags]:
            tags.append(text)
    return tags[:5]


def fallback_metadata(script_text: str) -> dict:
    """Sane defaults when Gemini is unavailable - never blocks an upload."""
    first = re.split(r"(?<=[.!?])\s+", script_text.strip())[0] if script_text else ""
    title = (first or "A Hard Truth About Your Mind").strip()
    if len(title) > YT_TITLE_MAX:
        # cut at the earliest natural pause so the title stays a complete thought
        cut = title[:YT_TITLE_MAX]
        pauses = [p for p in (cut.find(","), cut.find(":"), cut.rfind(" - ")) if p > 25]
        title = cut[:min(pauses)].rstrip() if pauses else cut.rsplit(" ", 1)[0].rstrip()
    return {"title": title, "hashtags": DEFAULT_HASHTAGS, "source": "fallback"}


def generate_metadata(script_text: str, api_key: str | None) -> dict:
    """Gemini -> {'title': ..., 'hashtags': [...]}; falls back on any failure."""
    if not api_key:
        print("      GEMINI_API_KEY not set - using fallback metadata")
        return fallback_metadata(script_text)
    if not script_text:
        print("      no script text available - using fallback metadata")
        return fallback_metadata(script_text)

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("      google-genai not installed - using fallback metadata")
        return fallback_metadata(script_text)

    prompt = (
        "Write upload metadata for a faceless YouTube Shorts / Instagram Reels "
        "channel about philosophy and dark psychology.\n\n"
        f"VIDEO SCRIPT:\n{script_text}\n\n"
        "Return STRICT JSON only (no markdown, no commentary):\n"
        '{"title": "...", "hashtags": ["#...", "#..."]}\n\n'
        "RULES:\n"
        f"1. title: max {YT_TITLE_MAX} characters. First silently draft FIVE "
        "candidates, each from a different angle: (a) direct challenge, "
        "(b) curiosity gap, (c) contradiction, (d) surprising insight, "
        "(e) emotional tension. Then pick the single strongest by curiosity, "
        "emotional pull, clarity, and clickability, and output ONLY that "
        "winner. Style references: 'Lose Everything Tonight. Here's Why.', "
        "'Why You Can Never Read Anyone's Mind'. It must be a complete "
        "thought - no ellipses, no mid-sentence cuts, no emojis, no quotation "
        "marks, and never a promise the video cannot keep.\n"
        "2. hashtags: EXACTLY 3 to 5 niche tags. Each starts with #, uses only "
        "letters, no spaces. Mix broad (#philosophy, #psychology) with specific "
        "(#stoicism, #darkpsychology, #machiavellianism)."
    )

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=90000),  # 90 s per request
    )
    for model in resolve_models():
        for attempt in (1, 2):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=512,
                        response_mime_type="application/json",
                    ),
                )
                raw = (response.text or "").strip()
                raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
                data = json.loads(raw)
                title = str(data.get("title", "")).strip().strip('"')
                hashtags = _clean_hashtags(data.get("hashtags", []))
                if not title or len(hashtags) < 3:
                    raise ValueError(f"bad metadata shape (title={bool(title)}, "
                                     f"hashtags={len(hashtags)})")
                if len(title) > YT_TITLE_MAX:
                    title = title[:YT_TITLE_MAX - 1].rstrip() + "…"
                print(f"      metadata model: {model}")
                return {"title": title, "hashtags": hashtags, "source": model}
            except Exception as exc:
                if is_permanent_model_error(exc):
                    print(f"      {model}: skipped ({str(exc)[:70]})")
                    break
                if attempt == 1:
                    time.sleep(2)
                    continue
                print(f"      {model}: failed ({str(exc)[:70]})")
    print("      all Gemini models failed - using fallback metadata")
    return fallback_metadata(script_text)


def build_platform_text(meta: dict, script_text: str) -> tuple[str, str, str, list[str]]:
    """Derive (youtube_title, youtube_description, instagram_caption, yt_tags)."""
    title = re.sub(r"\s+", " ", meta["title"]).strip()
    hashtags = meta["hashtags"]

    yt_tags = [t.lstrip("#") for t in hashtags]
    for extra in ("shorts", "philosophy", "dark psychology", "stoicism", "mindset"):
        if extra not in [t.lower() for t in yt_tags]:
            yt_tags.append(extra)
    while sum(len(t) + 1 for t in yt_tags) > YT_TAGS_MAX_CHARS and len(yt_tags) > 3:
        yt_tags.pop()

    yt_description = (
        f"{title}\n\n{script_text}\n\n"
        f"{' '.join(hashtags)} #Shorts"
    )
    ig_caption = f"{title}\n\n{' '.join(hashtags)}"
    return title, yt_description, ig_caption, yt_tags


# ---------------------------------------------------------------------------
# YouTube Shorts upload (OAuth 2.0 refresh token - unattended, CI-safe)
# ---------------------------------------------------------------------------

def upload_youtube(video_path: Path, title: str, description: str,
                   tags: list[str], privacy: str) -> tuple[str, object]:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    client_id = _require_env("YT_CLIENT_ID")
    client_secret = _require_env("YT_CLIENT_SECRET")
    refresh_token = _require_env("YT_REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        # NOTE: no 'scopes' argument on purpose - Google returns the token with
        # its ORIGINAL full scope set on refresh; pinning scopes here can cause
        # a scope_mismatch error with upload-only tokens.
    )
    try:
        creds.refresh(Request())          # fail fast on a bad/expired token
    except Exception as exc:
        raise RuntimeError(
            "YouTube refresh token rejected - generate a fresh one with "
            f"tools/yt_refresh_token.py and update YT_REFRESH_TOKEN ({exc})")

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": tags,
            "categoryId": YT_CATEGORY_ID,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body,
                                      media_body=media)
    print(f"      uploading ({video_path.stat().st_size / (1024*1024):.1f} MB) ...")
    response = None
    while response is None:
        try:
            status, response = request.next_chunk(num_retries=5)
        except HttpError as exc:
            reason = ""
            try:
                reason = exc.resp.status, exc.error_details[0].get("reason", "")
            except Exception:
                reason = exc.resp.status
            raise RuntimeError(f"YouTube API error: {exc.resp.status} {reason}") from exc
        if status:
            print(f"      {int(status.progress() * 100):3d}% uploaded")
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"unexpected YouTube response: {response}")
    return video_id, youtube


def resolve_thumbnail(run_folder: Path, explicit: str | None) -> dict | None:
    """Thumbnail to attach, from step_thumbnail.py's manifest (or override).

    Returns {youtube, instagram, variant, timestamp} or None. Respects
    AUTO_UPLOAD_THUMBNAIL (env wins over the manifest's attach flag)."""
    env = os.getenv("AUTO_UPLOAD_THUMBNAIL", "").strip().lower()
    if env and env in ("0", "false", "off", "no"):
        print("      thumbnails: AUTO_UPLOAD_THUMBNAIL=false - review-only "
              "mode, nothing will be attached")
        return None

    thumb = {"youtube": None, "instagram": None, "variant": "",
             "timestamp": None}
    manifest_path = run_folder / "thumbnails" / "thumbnail_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            sel = manifest.get("selected") or {}
            if sel and not sel.get("attach", True) and not env:
                print("      thumbnails: manifest says review-only "
                      "(auto_upload_thumbnail=false) - not attaching")
                return None
            if sel.get("youtube"):
                thumb["youtube"] = run_folder / sel["youtube"]
            if sel.get("instagram"):
                thumb["instagram"] = run_folder / sel["instagram"]
            thumb["variant"] = sel.get("variant", "")
            info = (manifest.get("variants") or {}).get(sel.get("variant", "")) \
                or {}
            thumb["timestamp"] = info.get("frame_timestamp")
        except Exception as exc:
            print(f"      thumbnail manifest unreadable ({str(exc)[:60]})")
    else:
        print("      no thumbnail manifest found (step_thumbnail skipped?)")

    if explicit:                       # manual override beats everything
        p = Path(explicit)
        if p.is_file():
            thumb["youtube"] = p
            thumb["instagram"] = p
            thumb["variant"] = f"manual:{p.name}"
        else:
            print(f"      WARNING: --thumbnail file not found: {p}")

    for key in ("youtube", "instagram"):
        if thumb[key] and not thumb[key].is_file():
            print(f"      WARNING: {key} thumbnail missing: {thumb[key]}")
            thumb[key] = None
    return thumb if (thumb["youtube"] or thumb["instagram"]) else None


def set_youtube_thumbnail(service, video_id: str, thumb_path: Path,
                          variant: str, timestamp) -> bool:
    """thumbnails.set with LOUD, actionable failure logging (never silent)."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    size_kb = thumb_path.stat().st_size / 1024
    print(f"      thumbnail: setting '{thumb_path.name}' ({size_kb:.0f} KB, "
          f"variant: {variant or '?'}, "
          f"source frame t={timestamp if timestamp is not None else '?'}s) ...")
    try:
        media = MediaFileUpload(str(thumb_path), mimetype="image/jpeg")
        service.thumbnails().set(videoId=video_id,
                                 media_body=media).execute()
        print("      thumbnail: SET OK (YouTube)")
        return True
    except HttpError as exc:
        status = exc.resp.status
        reason = ""
        try:
            reason = exc.error_details[0].get("reason", "")
        except Exception:
            pass
        if status == 403:
            print("=" * 60)
            print("  THUMBNAIL NOT SET - YouTube returned 403 (forbidden)")
            print(f"  reason: {reason or 'forbidden'}")
            print("  This channel is most likely NOT enabled for custom")
            print("  thumbnails yet. Fix (one-time, ~2 minutes):")
            print("    1. Open https://www.youtube.com and sign in as the")
            print("       channel owner")
            print("    2. Go to youtube.com/verify (phone verification)")
            print("    3. Also check YouTube Studio > Settings > Channel >")
            print("       Feature eligibility > 'Features that require phone")
            print("       verification' -> enable custom thumbnails")
            print("  The video itself is LIVE; set the image manually at:")
            print(f"    https://studio.youtube.com/video/{video_id}/edit")
            print(f"  The generated file is saved at: {thumb_path}")
            print("=" * 60)
        else:
            print(f"      thumbnail: FAILED (HTTP {status} {reason}) - video "
                  f"is live; set it manually in Studio for {video_id}")
        return False
    except Exception as exc:
        print(f"      thumbnail: FAILED ({str(exc)[:90]}) - video is live; "
              "set it manually in Studio")
        return False


# ---------------------------------------------------------------------------
# Instagram Reels upload (instagrapi - session id preferred, password fallback)
# ---------------------------------------------------------------------------

# One FIXED device fingerprint: every run presents the exact same "phone" to
# Instagram. A brand-new random device each day (instagrapi's default) from a
# datacenter IP is a strong automation red flag - determinism is safer.
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
    """Stable UUID derived from a fixed seed (same device ids every run)."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"innerlogic-ig:{name}"))


def _ig_explain(exc: Exception) -> str:
    """Translate Instagram private-API errors into actionable English."""
    text = str(exc)
    low = text.lower()
    if ("challenge_required" in low or "challengeerror" in low
            or "checkpoint" in low):
        return ("Instagram requires a login challenge (checkpoint) - the "
                "GitHub runner's datacenter IP is flagged. Open Instagram in "
                "your browser, approve any 'Was this you?' prompt, then "
                "create a FRESH session cookie and update the IG_SESSIONID "
                "secret. Details: " + text[:150])
    if ("467" in text or "login_required" in low or "not logged in" in low
            or "please wait" in low and "login" in low):
        return ("Instagram rejected the session (467 / login required) - "
                "datacenter IPs are heavily restricted by Instagram. Make a "
                "fresh IG_SESSIONID cookie (browser > DevTools > Application "
                "> Cookies > instagram.com > sessionid) and update the "
                "secret; if challenges persist, the session must be minted "
                "from a residential IP. Details: " + text[:150])
    if "pleasewait" in low or "rate limit" in low or "429" in text:
        return ("Instagram rate limit hit - wait ~30-60 minutes and retry. "
                "Details: " + text[:150])
    return text[:200]


def upload_instagram(video_path: Path, caption: str,
                     cover_path: Path | None = None) -> str:
    from urllib.parse import unquote
    from instagrapi import Client

    client = Client()
    client.delay_range = [2, 6]          # human-ish pauses between calls
    client.request_timeout = 90          # reels uploads can be slow

    # deterministic device + ids so Instagram sees the same phone every run
    try:
        client.set_device(dict(_IG_DEVICE))
        client.set_locale("en_US")
        client.set_country("US")
        client.set_timezone_offset(0)
        client.set_uuids({
            "phone_id": _ig_uuid("phone"),
            "uuid": _ig_uuid("device"),
            "client_session_id": _ig_uuid("session"),
            "advertising_id": _ig_uuid("ads"),
            "device_id": _ig_uuid("device"),
        })
        client.set_user_agent(
            "Instagram 269.0.0.18.75 Android (30/11; 480dpi; 440x2400; "
            "Google; Pixel 4; Pixel 4; qcom; en_US; 410532554)")
    except Exception:
        pass                            # cosmetic - never block on pinning

    if IG_SESSION_FILE.is_file():
        try:
            client.load_settings(str(IG_SESSION_FILE))
            print("      reusing cached Instagram session settings")
        except Exception:
            pass

    # accept both the raw cookie value and a URL-encoded paste (%3A -> :)
    session_id = unquote(os.getenv("IG_SESSIONID", "").strip())
    session_json = os.getenv("IG_SESSION_JSON", "").strip()
    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()

    if session_json:
        # full session blob minted at home with tools/ig_make_session.py -
        # carries the trusted device fingerprint, the most 467-proof option
        import json as _json
        print("      logging in with IG_SESSION_JSON (home-created session "
              "- most stable)")
        try:
            client.set_settings(_json.loads(session_json))
        except Exception as exc:
            raise RuntimeError(
                "IG_SESSION_JSON is unreadable/corrupted "
                f"({str(exc)[:80]}) - regenerate it on YOUR computer with "
                "tools/ig_make_session.py and update the secret") from exc
        print("      session settings loaded")
    elif session_id:
        print("      logging in with IG_SESSIONID (recommended method)")
        try:
            client.login_by_sessionid(session_id)
        except Exception as exc:
            raise RuntimeError(
                "IG_SESSIONID login failed - "
                f"{_ig_explain(exc)}") from exc
    elif username and password:
        print("      logging in with IG_USERNAME + IG_PASSWORD")
        try:
            client.login(username, password)
        except Exception as exc:
            raise RuntimeError(
                "Instagram password login failed (cloud IPs often trigger a "
                "verification challenge). Create a session cookie in your "
                "browser and add it as the IG_SESSIONID secret instead. "
                f"{_ig_explain(exc)}") from exc
    else:
        raise RuntimeError("no Instagram credentials found")

    try:
        client.get_timeline_feed()        # cheap request: validates the session
    except Exception as exc:
        raise RuntimeError("Instagram session invalid - "
                           f"{_ig_explain(exc)}") from exc

    print(f"      uploading reel ({video_path.stat().st_size / (1024*1024):.1f} MB) ...")
    if cover_path is not None:
        print(f"      cover: {cover_path.name} (Reels cover image)")
    try:
        media = client.clip_upload(str(video_path), caption=caption,
                                   thumbnail=cover_path)
    except TypeError:
        # older instagrapi without the thumbnail kwarg - publish anyway
        print("      WARNING: installed instagrapi cannot set a cover image "
              "(no 'thumbnail' param) - uploading without it")
        media = client.clip_upload(str(video_path), caption=caption)
    except Exception as exc:
        raise RuntimeError(f"Instagram upload failed - "
                           f"{_ig_explain(exc)}") from exc
    try:                                   # cache the session for re-runs
        IG_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        client.dump_settings(str(IG_SESSION_FILE))
    except Exception:
        pass
    code = getattr(media, "code", None) or ""
    return f"https://www.instagram.com/reel/{code}/"


def _require_env(name: str) -> str:
    import os
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 3: publish final_short.mp4 to YouTube Shorts + Instagram Reels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python step3_upload.py\n"
            "  python step3_upload.py --run-id run_20260831_120000\n"
            "  python step3_upload.py --dry-run        # metadata only\n"
            "  python step3_upload.py --skip-instagram\n"
        ),
    )
    parser.add_argument("--run-id", help="run folder under --output-dir (default: newest)")
    parser.add_argument("--output-dir", default="output", help="base output directory")
    parser.add_argument("--video", help="explicit path to final_short.mp4")
    parser.add_argument("--privacy", default="",
                        help="YouTube privacy: public | unlisted | private "
                             "(default: YT_PRIVACY env, else public)")
    parser.add_argument("--dry-run", action="store_true",
                        help="generate + save metadata only, upload nothing")
    parser.add_argument("--skip-youtube", action="store_true", help="do not upload to YouTube")
    parser.add_argument("--skip-instagram", action="store_true", help="do not upload to Instagram")
    parser.add_argument("--thumbnail", default=None,
                        help="override the auto-selected thumbnail file "
                             "(used for both platforms)")
    parser.add_argument("--no-thumbnail", action="store_true",
                        help="do not attach any thumbnail this run")
    args = parser.parse_args()

    import os
    load_environment(None)                 # local .env; CI uses real env vars
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    print("=" * 60)
    print("  FACELESS CHANNEL PIPELINE - STEP 3")
    print("  metadata (Gemini) -> YouTube Shorts + Instagram Reels")
    print("=" * 60)

    video, run_folder, script_text = find_run(args.output_dir, args.run_id, args.video)
    print(f"[1/3] Video: {video}")
    if not script_text:
        print("      WARNING: no script.txt found - metadata will use defaults")

    print("[2/3] Generating title + hashtags with Gemini ...")
    meta = generate_metadata(script_text, api_key or None)
    yt_title, yt_description, ig_caption, yt_tags = build_platform_text(meta, script_text)
    meta_path = run_folder / "metadata.json"
    meta_path.write_text(json.dumps({
        **meta,
        "youtube": {"title": yt_title, "description": yt_description, "tags": yt_tags},
        "instagram": {"caption": ig_caption},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      title    : {yt_title}")
    print(f"      hashtags : {' '.join(meta['hashtags'])}")
    print(f"      saved    : {meta_path}")

    if args.dry_run:
        print("[3/3] DRY RUN - no uploads performed")
        return 0

    privacy = (args.privacy or os.getenv("YT_PRIVACY", "public")).strip().lower()
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"

    thumbnail = None if args.no_thumbnail \
        else resolve_thumbnail(run_folder, args.thumbnail)

    results: dict[str, str] = {}
    failures: dict[str, str] = {}
    thumb_results: dict[str, str] = {}
    yt_service = None
    yt_video_id = ""

    # YouTube --------------------------------------------------------------
    if args.skip_youtube:
        print("[3/3] YouTube: SKIPPED (--skip-youtube)")
    else:
        print(f"[3/3] YouTube upload (privacy: {privacy}) ...")
        if _has_creds("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
            try:
                yt_video_id, yt_service = upload_youtube(
                    video, yt_title, yt_description, yt_tags, privacy)
                results["YouTube"] = f"https://www.youtube.com/watch?v={yt_video_id}"
            except Exception as exc:
                failures["YouTube"] = str(exc)
        else:
            print("      SKIPPED: set YT_CLIENT_ID + YT_CLIENT_SECRET + "
                  "YT_REFRESH_TOKEN secrets (see README)")

    # Thumbnail attach (YouTube) - right after the upload, before IG.
    if yt_service and yt_video_id and thumbnail and thumbnail.get("youtube"):
        ok = set_youtube_thumbnail(yt_service, yt_video_id,
                                   thumbnail["youtube"],
                                   thumbnail.get("variant", ""),
                                   thumbnail.get("timestamp"))
        thumb_results["YouTube"] = "set" if ok else f"NOT set ({yt_video_id})"
    elif thumbnail is None:
        pass
    elif not thumbnail.get("youtube"):
        print("      thumbnail: no YouTube-resolution file - skipped")

    # Instagram --------------------------------------------------------------
    if args.skip_instagram:
        print("      Instagram: SKIPPED (--skip-instagram)")
    else:
        print("      Instagram upload ...")
        if _has_creds("IG_SESSIONID") or _has_creds("IG_USERNAME", "IG_PASSWORD"):
            try:
                results["Instagram"] = upload_instagram(
                    video, ig_caption, thumbnail["instagram"] if thumbnail else None)
                if thumbnail and thumbnail.get("instagram"):
                    thumb_results["Instagram"] = \
                        f"cover={thumbnail['instagram'].name}"
            except Exception as exc:
                failures["Instagram"] = str(exc)
        else:
            print("      SKIPPED: set IG_USERNAME + IG_PASSWORD (or IG_SESSIONID) "
                  "secrets (see README)")

    # Summary ----------------------------------------------------------------
    print()
    print("=" * 60)
    if results:
        for platform, url in results.items():
            print(f"  published on {platform}: {url}")
    if failures:
        for platform, error in failures.items():
            print(f"  FAILED on {platform}: {error}")
    if thumb_results:
        for platform, note in thumb_results.items():
            print(f"  thumbnail on {platform}: {note}")
    if thumbnail:
        print(f"  thumbnail variant: {thumbnail.get('variant', '?')} | "
              f"source frame t={thumbnail.get('timestamp')}s")
    if not results and not failures:
        print("  nothing uploaded (no platform credentials configured)")
    print("=" * 60)

    # record what happened next to the generated thumbnails for review
    try:
        manifest_path = run_folder / "thumbnails" / "thumbnail_manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["upload_result"] = {
                "urls": results, "thumbnails": thumb_results,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            manifest_path.write_text(json.dumps(
                manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if failures and not results:
        return 1                     # every attempted platform failed
    return 0


def _has_creds(*names: str) -> bool:
    import os
    return all(os.getenv(n, "").strip() for n in names)


if __name__ == "__main__":
    sys.exit(main())
