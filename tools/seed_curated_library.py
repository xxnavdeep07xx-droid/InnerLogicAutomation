#!/usr/bin/env python3
"""Pre-download the whole curated footage library into curated_library/cache/.

    python tools/seed_curated_library.py            # needs PEXELS_API_KEY
    python tools/seed_curated_library.py --refresh  # re-download missing/failed

Run this once on a machine with the PEXELS key so renders never wait on
downloads; CI downloads only the 6-9 clips a video actually uses.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv          # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import curated_library as cl            # noqa: E402
import pexels_bg                        # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache and download fresh copies")
    parser.add_argument("--force", action="store_true",
                        help="delete the cache first, then download")
    args = parser.parse_args()

    entries = cl.load_entries()
    if not entries:
        print("no library entries found - check curated_library/library.json")
        return 1
    if args.force and cl.CACHE_DIR.is_dir():
        for f in cl.CACHE_DIR.glob("*.mp4"):
            f.unlink()
    cl.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    used: set[int] = set()
    ok, failed = 0, []
    for i, entry in enumerate(entries, 1):
        dest = cl.CACHE_DIR / f"{entry['id']}.mp4"
        if dest.is_file() and dest.stat().st_size > 50_000 and not args.refresh:
            print(f"[{i:2d}/{len(entries)}] {entry['id']:<16} cached")
            ok += 1
            continue
        got = pexels_bg.search_and_download(entry["query"], dest, used)
        if got:
            ok += 1
            print(f"[{i:2d}/{len(entries)}] {entry['id']:<16} ready "
                  f"({dest.stat().st_size / (1024 * 1024):.1f} MB)")
        else:
            failed.append(entry["id"])
            print(f"[{i:2d}/{len(entries)}] {entry['id']:<16} FAILED")

    print(f"\n{ok}/{len(entries)} clips cached at {cl.CACHE_DIR}")
    if failed:
        print("failed entries:", ", ".join(failed))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
