#!/usr/bin/env python3
"""
publish_release.py - stage the day's finished video for the manual-upload site.

Runs inside GitHub Actions after main.py renders. Does three things:
  1. Creates a GitHub Release `daily-YYYYMMDD` (public repo -> public asset
     URLs) carrying final_short.mp4, the hook thumbnail, and the platform
     metadata as a machine-readable JSON block in the release body.
  2. Regenerates site/data/manifest.json by listing every `daily-*` release
     (newest first). The site and the auto-fill bookmarklets read this file,
     so it is committed to main.
  3. Prints a summary line for the run log.

Auth: the workflow's own GITHUB_TOKEN (env GH_TOKEN) with contents:write -
no personal PAT needed. Repo taken from GITHUB_REPOSITORY.

Usage:  python tools/publish_release.py --run-id run_20260904_131025
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def gh(method: str, url: str, token: str, payload: dict | None = None,
       raw: bytes | None = None, ctype: str = ""):
    data = raw if raw is not None else (
        json.dumps(payload).encode() if payload is not None else None)
    req = Request(url, method=method, data=data)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if ctype:
        req.add_header("Content-Type", ctype)
    with urlopen(req, timeout=300) as r:
        body = r.read()
        return json.loads(body) if body else {}


def find_run(base: Path, run_id: str | None) -> Path:
    if run_id:
        folder = base / run_id
        if not folder.is_dir():
            sys.exit(f"run folder not found: {folder}")
        return folder
    cands = [d for d in base.iterdir()
             if d.is_dir() and (d / "final_short.mp4").is_file()]
    if not cands:
        sys.exit("no run folder with final_short.mp4 under output/")
    return max(cands, key=lambda d: d.stat().st_mtime)


def load_meta(run_folder: Path) -> dict:
    """metadata.json written by step3_upload's generator, else defaults."""
    p = run_folder / "metadata.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  WARNING: metadata.json unreadable ({exc})")
    title = run_folder.name
    return {"title": title, "youtube": {"title": title, "description": "",
            "tags": []}, "instagram": {"caption": ""}, "hashtags": []}


def load_thumb(run_folder: Path) -> Path | None:
    tm = run_folder / "thumbnails" / "thumbnail_manifest.json"
    if tm.is_file():
        try:
            m = json.loads(tm.read_text(encoding="utf-8"))
            variant = m.get("config", {}).get("thumbnail_variant", "hook")
            rel = (m.get("variants", {}).get(variant, {})
                   .get("files", {}).get("youtube"))
            if rel:
                p = run_folder / rel
                if p.is_file():
                    return p
        except Exception:
            pass
    th = run_folder / "thumbnails"
    if th.is_dir():
        jpgs = sorted(th.glob("*_youtube.jpg"))
        if jpgs:
            return jpgs[0]
    return None


def create_release(run_folder: Path, repo: str, token: str) -> dict:
    meta = load_meta(run_folder)
    yt = meta.get("youtube", {})
    ig = meta.get("instagram", {})
    video = run_folder / "final_short.mp4"
    thumb = load_thumb(run_folder)

    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    tag = f"daily-{day}"
    n = 2
    while True:
        try:
            gh("GET", f"https://api.github.com/repos/{repo}"
                      f"/releases/tags/{tag}", token)
            tag = f"daily-{day}-r{n}"
            n += 1
        except Exception:
            break                                   # 404 -> tag is free

    entry = {
        "tag": tag,
        "run_id": run_folder.name,
        "created_at": datetime.now(timezone.utc)
                             .isoformat(timespec="seconds"),
        "title": yt.get("title") or meta.get("title", tag),
        "description": yt.get("description", ""),
        "tags": yt.get("tags", []),
        "ig_caption": ig.get("caption", ""),
        "hashtags": meta.get("hashtags", []),
        "video_file": video.name,
        "thumb_file": thumb.name if thumb else "",
    }
    body = ("Automated daily drop - ready for manual upload.\n\n"
            "```json\n" + json.dumps(entry, ensure_ascii=False, indent=1)
            + "\n```\n")

    rel = gh("POST", f"https://api.github.com/repos/{repo}/releases", token,
             {"tag_name": tag, "name": entry["title"][:80],
              "body": body, "prerelease": True})
    print(f"  release created: {tag}")

    up = rel["upload_url"].split("{")[0]
    assets = [("application/octet-stream", video)]
    if thumb:
        assets.append(("image/jpeg", thumb))
    for ctype, f in assets:
        gh("POST", f"{up}?name={f.name}", token, raw=f.read_bytes(),
           ctype=ctype)
        print(f"  asset uploaded: {f.name} "
              f"({f.stat().st_size / (1024 * 1024):.1f} MB)")

    entry["video_url"] = \
        f"https://github.com/{repo}/releases/download/{tag}/{video.name}"
    entry["thumb_url"] = (f"https://github.com/{repo}/releases/download/"
                          f"{tag}/{thumb.name}") if thumb else ""
    return entry


def build_manifest(repo: str, token: str, newest: dict | None) -> dict:
    rels = gh("GET", f"https://api.github.com/repos/{repo}/releases"
                     "?per_page=50", token)
    videos = []
    for r in rels:
        tag = r.get("tag_name", "")
        if not tag.startswith("daily-"):
            continue
        body = r.get("body", "")
        if "```json" not in body:
            continue
        try:
            blob = body.split("```json", 1)[1].split("```", 1)[0]
            entry = json.loads(blob)
        except Exception:
            continue
        base = f"https://github.com/{repo}/releases/download/{tag}"
        entry.setdefault("video_url", f"{base}/{entry.get('video_file', '')}")
        entry.setdefault("thumb_url", f"{base}/{entry.get('thumb_file', '')}"
                         if entry.get("thumb_file") else "")
        entry["release_url"] = r.get("html_url", "")
        videos.append(entry)
    videos.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    if newest and (not videos or videos[0].get("tag") != newest.get("tag")):
        videos.insert(0, newest)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "drop_time_utc": "15:00",
        "repo": repo,
        "videos": videos,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-dir", default=str(ROOT / "output"))
    a = ap.parse_args()

    token = (os.getenv("GH_TOKEN", "")
             or os.getenv("GH_RELEASE_TOKEN", "")).strip()
    repo = os.getenv("GITHUB_REPOSITORY",
                     "xxnavdeep07xx-droid/InnerLogicAutomation").strip()
    if not token:
        sys.exit("GH_TOKEN missing (CI: pass ${{ github.token }})")

    run_folder = find_run(Path(a.output_dir), a.run_id)
    print(f"  staging run: {run_folder.name}")
    newest = create_release(run_folder, repo, token)
    manifest = build_manifest(repo, token, newest)

    out = ROOT / "site" / "data" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"  manifest written: {out} ({len(manifest['videos'])} videos)")
    print(f"  LATEST: {newest['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
