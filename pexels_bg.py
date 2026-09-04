#!/usr/bin/env python3
"""
pexels_bg.py - fetch vertical b-roll clips from the free Pexels API.

Takes the search_queries list produced by step 1 (Gemini), searches Pexels
for each term (portrait/9:16 videos), downloads one clip per term into the
run folder, and returns the local paths in story order. Step 2 then hard-cuts
between these clips at the concept-transition timestamps.

Everything here is best-effort by design: a missing key, rate limits, network
errors or zero results simply return fewer (or no) clips - the caller falls
back to the static background, so the pipeline can never break.

Setup
-----
    1. Create a free account at https://www.pexels.com/api/
    2. Put your key in .env:  PEXELS_API_KEY=...
       (on GitHub Actions: repository secret PEXELS_API_KEY)

Env vars
--------
    PEXELS_API_KEY    required to enable dynamic backgrounds
    PEXELS_PER_PAGE   optional, results per search (default 15)
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

try:
    import requests
except ImportError:                       # requirements.txt installs it anyway
    requests = None

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
MIN_CLIP_HEIGHT = 1080                    # reject anything below 1080-class
DL_TIMEOUT = 120                          # seconds per download


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:40] or "query"


def _score_file(f: dict) -> tuple:
    """Higher = better download candidate (mp4, portrait, ~1080-1920 tall)."""
    height = int(f.get("height") or 0)
    width = int(f.get("width") or 0)
    is_mp4 = f.get("file_type") == "video/mp4"
    portrait = height > width
    h_score = -abs(height - 1536)         # closest to 1080p-class 9:16 wins
    return (is_mp4, portrait, min(height, 1920), h_score)


def _pick_video(videos: list[dict], used_ids: set[int]) -> dict | None:
    """Choose the best unused video: usable portrait file, longest first."""
    scored = []
    for v in videos:
        vid = int(v.get("id") or 0)
        if vid in used_ids:
            continue
        duration = float(v.get("duration") or 0)
        files = [f for f in v.get("video_files", [])
                 if int(f.get("height") or 0) >= MIN_CLIP_HEIGHT
                 and int(f.get("width") or 0) >= 540]
        if not files:
            continue
        files.sort(key=_score_file, reverse=True)
        scored.append((duration, vid, files[0]))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1]))     # prefer longer clips
    _, vid, chosen = scored[0]
    used_ids.add(vid)
    return chosen


def _download(url: str, dest: Path) -> bool:
    """Stream a clip to dest; True only if a sane mp4 landed on disk."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=DL_TIMEOUT) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
        if tmp.stat().st_size < 50_000:            # <50 KB = broken response
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(dest)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _new_session() -> requests.Session | None:
    """Authorized session, or None (with a printed reason) if unusable."""
    if requests is None:
        print("      pexels: 'requests' not installed - static background")
        return None
    api_key = (os.getenv("PEXELS_API_KEY", "") or "").strip()
    if not api_key:
        print("      pexels: PEXELS_API_KEY not set - static background")
        return None
    session = requests.Session()
    session.headers.update({"Authorization": api_key})
    return session


def search_best_file(session: "requests.Session", query: str,
                     used_ids: set[int]) -> dict | None:
    """Search Pexels for `query` and return the best unused portrait file."""
    per_page = int(os.getenv("PEXELS_PER_PAGE", "15") or 15)
    try:
        r = session.get(PEXELS_SEARCH_URL,
                        params={"query": query, "orientation": "portrait",
                                "per_page": per_page},
                        timeout=30)
        r.raise_for_status()
        videos = r.json().get("videos", [])
    except Exception as exc:
        print(f"      pexels: '{query}' search failed: {str(exc)[:80]}")
        return None
    chosen = _pick_video(videos, used_ids)
    if not chosen:
        print(f"      pexels: '{query}' no usable portrait clip")
    return chosen


def search_and_download(query: str, dest: Path,
                        used_ids: set[int] | None = None) -> Path | None:
    """One query -> one vertical clip at `dest` (shared by all callers).

    Returns dest on success, None on any failure (caller falls back)."""
    used_ids = used_ids if used_ids is not None else set()
    session = _new_session()
    if session is None:
        return None
    if dest.is_file() and dest.stat().st_size > 50_000:
        return dest                            # already cached
    chosen = search_best_file(session, query, used_ids)
    if not chosen:
        return None
    if _download(chosen.get("link", ""), dest):
        size_mb = dest.stat().st_size / (1024 * 1024)
        print(f"      pexels: '{query}' -> {dest.name} "
              f"({chosen.get('width')}x{chosen.get('height')}, {size_mb:.1f} MB)")
        return dest
    print(f"      pexels: '{query}' download failed")
    return None


def fetch_backgrounds(queries: list[str], out_dir: Path,
                      max_clips: int = 4) -> list[Path]:
    """Download one vertical clip per search query (best-effort).

    Returns the downloaded clip paths in story order. An empty list means
    the caller should use the static background fallback."""
    queries = [q for q in queries if isinstance(q, str) and q.strip()][:max_clips]
    if not queries:
        return []
    session = _new_session()
    if session is None:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    used_ids: set[int] = set()            # never reuse the same video twice
    clips: list[Path] = []

    for i, query in enumerate(queries, 1):
        dest = out_dir / f"clip_{i:02d}_{_slug(query)}.mp4"
        if dest.is_file() and dest.stat().st_size > 50_000:
            print(f"      [{i}/{len(queries)}] '{query}' -> cached {dest.name}")
            clips.append(dest)
            continue
        chosen = search_best_file(session, query, used_ids)
        if chosen and _download(chosen.get("link", ""), dest):
            size_mb = dest.stat().st_size / (1024 * 1024)
            print(f"      [{i}/{len(queries)}] '{query}' -> {dest.name} "
                  f"({chosen.get('width')}x{chosen.get('height')}, {size_mb:.1f} MB)")
            clips.append(dest)
        else:
            print(f"      [{i}/{len(queries)}] '{query}' unavailable")
        time.sleep(0.4)                       # stay friendly with rate limits

    if clips:
        print(f"      pexels: {len(clips)}/{len(queries)} clips ready")
    return clips
