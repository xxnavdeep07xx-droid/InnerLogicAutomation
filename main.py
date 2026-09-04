#!/usr/bin/env python3
"""
main.py - one-command entry point for the full pipeline.

    Gemini script  ->  edge-tts voiceover + word timings  ->  final_short.mp4

This is the command the GitHub Actions daily workflow runs, and the one you
use locally. It chains the two stages:

    step 1  step1_generate.py      (Gemini + edge-tts)
    step 2  step2_render_video.py  (moviepy: 9:16 crop, karaoke captions)

Usage
-----
    python main.py                       # full run: random topic, Gemini script
    python main.py --topic stoicism      # force a topic (or set VIDEO_TOPIC env)
    python main.py --script-file s.txt   # skip Gemini (test the render chain)
    python main.py --limit 8             # quick 8s preview render
    python main.py --mixed-case          # keep normal caption casing

Configuration (environment or .env - see .env.example)
------------------------------------------------------
    GEMINI_API_KEY     required for script generation (GitHub Secret in CI)
    VIDEO_TOPIC        optional: fixed topic for every run (GitHub Variable)
    GEMINI_MODEL       optional: force one Gemini model
    EDGE_TTS_VOICE     optional: voice (default en-US-AndrewMultilingualNeural)
    EDGE_TTS_RATE      optional: speaking rate, e.g. "+15%" to hit ~40 s
    BACKGROUND_VIDEO   optional: path to your background video

Background video resolution order
---------------------------------
    1. --background argument / BACKGROUND_VIDEO env (forces the static bg)
    2. DYNAMIC (default): Gemini's search_queries -> one Pexels 9:16 clip
       per query (needs PEXELS_API_KEY) -> hard cuts between scenes at the
       concept transitions (see step2_render_video.py)
    3. background.mp4 in the project root
    4. assets/background.mp4 (shipped with the repo - animated dark gradient)

Outputs: output/run_<timestamp>/
    script.txt, voiceover.mp3, word_timings.json, backgrounds/ (pexels),
    _music_bed.wav, final_short.mp4
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Animated dark gradient - philosophy / dark-psychology channel aesthetic.
BG_COLORS = ["0x0d0d1c", "0x2a1b4e", "0x10263a", "0x1d0f33"]
BG_SECONDS = 45        # covers a ~40 s voiceover; step 2 loops it if longer
BG_FPS = 30


def run(cmd: list[str]) -> None:
    """Run a subprocess, streaming its output (never hides progress)."""
    print(f"\n>>> {' '.join(str(c) for c in cmd)}\n")
    try:
        subprocess.run(cmd, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nERROR: command failed with exit code {exc.returncode}: "
                 f"{' '.join(str(c) for c in cmd)}")


def generate_background(dest: Path) -> Path:
    """Last-resort background: animated dark gradient via ffmpeg's lavfi."""
    if dest.is_file() and dest.stat().st_size > 0:
        return dest                                   # cached from a previous run
    dest.parent.mkdir(parents=True, exist_ok=True)
    colors = ":".join(f"c{i}={c}" for i, c in enumerate(BG_COLORS))
    seed = random.randint(1, 99999)
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", (f"gradients=s={1080}x{1920}:{colors}:nb_colors={len(BG_COLORS)}"
               f":speed=0.012:seed={seed}"),
        "-t", str(BG_SECONDS),
        "-r", str(BG_FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p",
        str(dest),
    ]
    print("      no background video found - generating an animated dark gradient")
    run(cmd)
    return dest


def resolve_background(explicit: str | None) -> Path:
    """Find the background video, generating a default one if needed."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.getenv("BACKGROUND_VIDEO", "").strip()
    if env:
        candidates.append(Path(env))
    candidates += [ROOT / "background.mp4", ROOT / "assets" / "background.mp4"]
    for candidate in candidates:
        if candidate.is_file():
            print(f"      background: {candidate}")
            return candidate
    return generate_background(ROOT / "output" / "_backgrounds" / "auto_dark_gradient.mp4")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full pipeline: Gemini script + voiceover -> final 1080x1920 short",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py\n"
            '  python main.py --topic "the shadow self"\n'
            "  python main.py --script-file sample_script.txt --limit 8\n"
            "  python main.py --background my_bg.mp4 --mixed-case\n"
        ),
    )
    # step 1 options
    parser.add_argument("--topic", help="topic for today's video (default: VIDEO_TOPIC env, else random)")
    parser.add_argument("--script-file", help="use a ready-made script file instead of Gemini")
    parser.add_argument("--beats-file", help="use a ready-made beats JSON file (v2 mode, skips Gemini)")
    parser.add_argument("--mode", choices=("beats", "classic"), default=None,
                        help="script style: 'beats' (v2: emotion/emphasis metadata + "
                             "hook gate) or 'classic' (legacy). Default: SCRIPT_MODE "
                             "env, else beats")
    parser.add_argument("--tts-backend", choices=("edge", "elevenlabs", "playht"),
                        default=None, help="voice backend (default: TTS_BACKEND env, else edge)")
    parser.add_argument("--run-id", help="output folder name (default: run_<timestamp>)")
    parser.add_argument("--output-dir", default="output", help="base output directory")
    # step 2 options
    parser.add_argument("--background", help="background video path (see resolution order in docstring)")
    parser.add_argument("--font", help="path to a bold .ttf font (default: auto-detect)")
    parser.add_argument("--max-words", type=int, default=4, help="max words per caption group (default: 4)")
    parser.add_argument("--subtitle-y", type=float, default=None, help="legacy caption vertical position 0=top 1=bottom")
    parser.add_argument("--caption-y-pct", type=float, default=None,
                        help="EXPLICIT caption vertical anchor override (beats the "
                             "platform preset; default: target_platform preset)")
    parser.add_argument("--target-platform", choices=("youtube", "instagram", "tiktok", "generic"),
                        default=None,
                        help="which platform's bottom UI this export must clear "
                             "(default: TARGET_PLATFORM env, else generic)")
    parser.add_argument("--bottom-clearance-pct", type=float, default=None,
                        help="EXPLICIT bottom clearance override (beats the platform preset)")
    parser.add_argument("--caption-font-size-pct", type=float, default=None,
                        help="caption font size as a fraction of frame width (default 0.065)")
    parser.add_argument("--title-card-font-size-pct", type=float, default=None,
                        help="hook title-card font size as a fraction of frame width (default 0.075)")
    parser.add_argument("--hook-style", choices=("title_card", "word_by_word", "both"),
                        default=None, help="beat[0] hook presentation (default: title_card)")
    parser.add_argument("--safe-zone-guides", action="store_true",
                        help="draw platform-UI safe-zone rectangles (debug render)")
    parser.add_argument("--highlight", default="#F5D90A", help="karaoke accent color, or 'none'")
    parser.add_argument("--emphasis-color", default="#FF3D2E", help="emphasis-word color (v2)")
    parser.add_argument("--sfx", choices=("auto", "off"), default=None,
                        help="cut whoosh + emphasis ding layer (default: SFX env, else auto)")
    parser.add_argument("--no-hook-overlay", action="store_true", help="disable the bold hook text overlay (v2)")
    parser.add_argument("--mixed-case", action="store_true", help="keep normal casing (captions are ALL CAPS by default)")
    parser.add_argument("--fps", type=int, default=30, help="output frame rate (default: 30)")
    parser.add_argument("--preset", default="medium", help="x264 preset (default: medium)")
    parser.add_argument("--threads", type=int, default=0, help="encoder threads (default: all cores)")
    parser.add_argument("--limit", type=float, default=0, help="render only the first N seconds (preview)")
    # thumbnails (step 2.6)
    parser.add_argument("--thumbnail-variant", choices=("hook", "midpoint", "clean"),
                        default=None,
                        help="which thumbnail variant becomes the upload default "
                             "(default: THUMBNAIL_VARIANT env, else hook)")
    parser.add_argument("--skip-thumbnails", action="store_true",
                        help="skip thumbnail generation entirely")
    args = parser.parse_args()

    print("=" * 60)
    print("  FACELESS CHANNEL PIPELINE - FULL RUN")
    print("  step 1: script + voiceover | step 2: video render")
    print("=" * 60)

    if args.tts_backend:
        os.environ["TTS_BACKEND"] = args.tts_backend   # step1 subprocess inherits

    # ---- step 1: Gemini script -> voiceover -> word_timings.json -----------
    step1_cmd = [sys.executable, "step1_generate.py", "--output-dir", args.output_dir]
    topic = args.topic or os.getenv("VIDEO_TOPIC", "").strip()
    if topic:
        step1_cmd += ["--topic", topic]
    if args.script_file:
        step1_cmd += ["--script-file", args.script_file]
    if args.beats_file:
        step1_cmd += ["--beats-file", args.beats_file]
    if args.mode:
        step1_cmd += ["--mode", args.mode]
    if args.run_id:
        step1_cmd += ["--run-id", args.run_id]
    run(step1_cmd)

    # ---- step 2: render final_short.mp4 ------------------------------------
    # NOTE: no --background is passed unless the user explicitly set one -
    # step 2 then prefers DYNAMIC Pexels backgrounds (one clip per Gemini
    # search_query, hard-cut at the concept transitions) and only falls
    # back to the static gradient when Pexels is unavailable.
    explicit_bg = bool(args.background) or bool(os.getenv("BACKGROUND_VIDEO", "").strip())
    step2_cmd = [
        sys.executable, "step2_render_video.py",
        "--output-dir", args.output_dir,
        "--max-words", str(args.max_words),
        "--highlight", args.highlight,
        "--emphasis-color", args.emphasis_color,
        "--fps", str(args.fps),
        "--preset", args.preset,
    ]
    # caption vertical anchor: only forwarded when explicitly set, so step 2
    # can apply its platform-aware preset (target_platform) otherwise
    if args.caption_y_pct is not None:
        step2_cmd += ["--caption-y-pct", str(args.caption_y_pct)]
    if args.subtitle_y is not None:
        step2_cmd += ["--subtitle-y", str(args.subtitle_y)]
    if args.target_platform:
        step2_cmd += ["--target-platform", args.target_platform]
    if args.bottom_clearance_pct is not None:
        step2_cmd += ["--bottom-clearance-pct", str(args.bottom_clearance_pct)]
    if args.caption_font_size_pct is not None:
        step2_cmd += ["--caption-font-size-pct", str(args.caption_font_size_pct)]
    if args.title_card_font_size_pct is not None:
        step2_cmd += ["--title-card-font-size-pct", str(args.title_card_font_size_pct)]
    if args.hook_style:
        step2_cmd += ["--hook-style", args.hook_style]
    if args.safe_zone_guides:
        step2_cmd += ["--safe-zone-guides"]
    if args.sfx:
        step2_cmd += ["--sfx", args.sfx]
    if args.no_hook_overlay:
        os.environ["HOOK_OVERLAY"] = "off"   # step2 subprocess inherits this
    if explicit_bg:
        background = resolve_background(args.background)
        step2_cmd += ["--background", str(background)]
    if args.font:
        step2_cmd += ["--font", args.font]
    if args.mixed_case:
        step2_cmd += ["--mixed-case"]
    if args.threads:
        step2_cmd += ["--threads", str(args.threads)]
    if args.limit:
        step2_cmd += ["--limit", str(args.limit)]
    if args.run_id:
        step2_cmd += ["--run-id", args.run_id]   # newest folder would match anyway
    run(step2_cmd)

    # ---- step 2.6: thumbnails (best-effort - never blocks publishing) ------
    if args.skip_thumbnails:
        print("\n(thumbnails skipped by --skip-thumbnails)")
    else:
        thumb_cmd = [sys.executable, "step_thumbnail.py",
                     "--output-dir", args.output_dir]
        if args.thumbnail_variant:
            thumb_cmd += ["--variant", args.thumbnail_variant]
        if args.run_id:
            thumb_cmd += ["--run-id", args.run_id]
        try:
            run(thumb_cmd)
        except SystemExit:
            print("\nWARNING: thumbnail generation failed - continuing "
                  "without thumbnails (upload proceeds without one)")

    print()
    print("=" * 60)
    print("  PIPELINE COMPLETE")
    print("  video     : output/<run_id>/final_short.mp4")
    print("  thumbnails: output/<run_id>/thumbnails/ (variants + manifest)")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
